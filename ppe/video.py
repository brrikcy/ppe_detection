"""Video pipeline: track people on every frame -> check PPE on a schedule -> annotate -> encode."""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import cv2
from PIL import Image

from .analyzer import PPEAnalyzer, WorkerResult
from .annotate import draw_results
from .media import FrameReader, FrameWriter
from .status import PersonStatus

log = logging.getLogger(__name__)

EDGE_PX = 2  # a box this close to the frame edge is (probably) cut off
ARRIVAL_CUTOFF_SEC = 0.5  # no arrival check when the next regular check is this close
CHECKS_IN_FLIGHT = 2  # check frames sent to Moondream at once (calls stay capped by parallel_requests)
BUFFER_BYTES = 512 * 1024 * 1024  # frames held back while their check is with Moondream
LABEL_DELAY_SEC = 1.0  # live mode: longest a frame waits for its check before it's shown without it
QUEUE_FRAMES = 8  # frames between the reader, tracker and writer threads
MAX_LAG_SEC = 0.5  # live mode: further behind schedule than this, playback restarts its clock
LIVE_MAX_HEIGHT = 1080  # live mode: taller video (e.g. 4K) is scaled down to this while decoding


def _at_edge(box, width: int, height: int) -> bool:
    x1, y1, x2, y2 = box
    return x1 <= EDGE_PX or y1 <= EDGE_PX or x2 >= width - EDGE_PX or y2 >= height - EDGE_PX


class _Prefetch:
    """Decodes frames on a background thread, a few ahead of the tracker."""

    def __init__(self, reader: FrameReader, limit: int):
        self._reader, self._limit = reader, limit
        self._q: queue.Queue = queue.Queue(maxsize=QUEUE_FRAMES)
        self._stop = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="read", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            n = 0
            while not self._stop.is_set() and (self._limit <= 0 or n < self._limit):
                frame = self._reader.read()
                if frame is None:
                    break
                self._put(frame)
                n += 1
        except BaseException as exc:  # handed to the tracker thread by get()
            self._error = exc
        finally:
            self._put(None)

    def _put(self, item) -> None:
        while not self._stop.is_set():
            try:
                self._q.put(item, timeout=0.1)
                return
            except queue.Full:
                continue

    def get(self):
        """The next frame, or None at the end."""
        frame = self._q.get()
        if frame is None and self._error is not None:
            raise self._error
        return frame

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1)


class _Output:
    """Draws and writes frames on a background thread, in order.

    With `realtime`, frames go out at the video's own speed, like a camera feed; after a
    stall, playback carries on from where it is instead of fast-forwarding to catch up.
    """

    def __init__(self, writer: FrameWriter, fps: float, total: int, realtime: bool,
                 on_frame: Callable | None, on_progress: Callable | None):
        self._writer, self._fps, self._total, self._realtime = writer, fps, total, realtime
        self._on_frame, self._on_progress = on_frame, on_progress
        self._q: queue.Queue = queue.Queue(maxsize=QUEUE_FRAMES)
        self._stop = threading.Event()
        self._error: BaseException | None = None
        self.written = 0
        self._thread = threading.Thread(target=self._run, name="write", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        clock = None  # monotonic time of frame 0 on the playback schedule
        try:
            while not self._stop.is_set():
                try:
                    item = self._q.get(timeout=0.1)
                except queue.Empty:
                    continue
                if item is None:
                    return
                idx, frame, display = item
                annotated = draw_results(frame, display)
                if self._realtime:
                    now = time.monotonic()
                    if clock is None or now - (clock + idx / self._fps) > MAX_LAG_SEC:
                        clock = now - idx / self._fps
                    else:
                        time.sleep(max(0.0, clock + idx / self._fps - now))
                self._writer.write(annotated)
                if self._on_frame:
                    self._on_frame(idx, annotated)
                self.written = done = idx + 1
                if self._on_progress and (done % 10 == 0 or done == self._total):
                    self._on_progress(done, self._total)
        except BaseException as exc:  # handed to the tracker thread by put() / close()
            self._error = exc

    def _check(self) -> None:
        if self._error is not None:
            raise self._error

    def put(self, idx: int, frame, display: list[WorkerResult]) -> None:
        while True:
            self._check()
            try:
                self._q.put((idx, frame, display), timeout=0.1)
                return
            except queue.Full:
                continue

    def close(self) -> None:
        """Waits until every frame is written."""
        while self._thread.is_alive():
            try:
                self._q.put(None, timeout=0.1)
                break
            except queue.Full:
                continue
        self._thread.join()
        self._check()

    def abort(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


def process_video(
    input_path: str | Path,
    output_path: str | Path,
    analyzer: PPEAnalyzer,
    max_seconds: float | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    on_analysed: Callable[..., None] | None = None,
    people_factory: Callable[[float], object] | None = None,
    on_frame: Callable[[int, "cv2.typing.MatLike"], None] | None = None,
    on_check: Callable[[int, int], None] | None = None,
    on_start: Callable[[int, int], None] | None = None,
    live: bool = False,
    max_label_delay_sec: float = LABEL_DELAY_SEC,
) -> dict:
    """Annotate `input_path` into `output_path` and return the report dict.

    Every frame, the person tracker made by `people_factory(fps)` (default: YOLO11s +
    BoT-SORT, see ppe.people) gives the tracked workers. Every analysis interval, each
    worker big enough to judge and tracked for at least `min_check_age_sec` gets a PPE
    check; a worker who appears between checks is checked as soon as they qualify (and
    are fully in frame), unless the next regular check is close. The video shows each
    worker's status voted over their recent checks (ppe.status); the same voted status
    is stored with every check in the report.

    on_progress(frames_done, total_frames) is called periodically while frames are written.
    on_analysed(timeline_entry, frame_bgr, results, display) is called after each check frame,
    with the raw check results and the voted boxes drawn in the video.
    on_frame(frame_index, annotated_bgr) is called for every frame written (for live previews).
    on_check(frame_index, workers) is called when the number of workers being checked changes.
    on_start(width, height) is called once with the size frames are processed at; worker boxes
    are in these pixels (smaller than the video's own size when live mode scales it down).
    on_progress and on_frame are called from the writer thread, the others from the caller's.

    Decoding, tracking and drawing + encoding run on their own threads. Tracking doesn't
    wait for Moondream: while a check frame is with Moondream, the next frames are read and
    tracked, and held back until the check's results are in.
    - By default frames are held back as long as it takes (up to BUFFER_BYTES), then drawn
      with the statuses as of that frame, so the output is the same as checking each frame
      before moving on.
    - With `live`, frames go out at the video's own speed, and a frame waits at most
      `max_label_delay_sec` for its check; if Moondream is slower, the frame is shown with
      the statuses so far and the results apply from the frame where they arrive. A check
      is skipped while CHECKS_IN_FLIGHT others are still out, so they never pile up. Video
      taller than LIVE_MAX_HEIGHT is processed (and written) at that height.
    """
    input_path, output_path = Path(input_path), Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # In live mode 4K is too slow to decode, draw and encode at the video's speed; 1080p is plenty.
    reader = FrameReader(input_path, max_height=LIVE_MAX_HEIGHT if live else None)
    fps, width, height, total = reader.fps, reader.width, reader.height, reader.frame_count
    if on_start:
        on_start(width, height)
    if max_seconds:
        total = min(total, int(max_seconds * fps)) if total > 0 else int(max_seconds * fps)

    cfg = analyzer.config
    tun = cfg.tuning
    step = max(1, int(round(cfg.analysis_interval_sec * fps)))
    arrival_cutoff = min(step // 2, max(1, int(round(ARRIVAL_CUTOFF_SEC * fps))))
    min_check_h = tun.min_check_height(height)
    min_age = int(round(tun.min_check_age_sec * fps))
    if live:
        max_buffered = max(1, int(round(max_label_delay_sec * fps)))
    else:
        max_buffered = max(int(fps), BUFFER_BYTES // max(1, width * height * 3))
    log.info("%s: %dx%d @ %.1ffps, %d frames, checking every %d frames (%s decoding%s)",
             input_path.name, width, height, fps, total, step, reader.decoder, ", live" if live else "")

    if people_factory is None:
        from .people import DEFAULT_MODEL, MODELS_DIR, YoloPersonTracker

        model = MODELS_DIR / tun.person_model if tun.person_model else Path(cfg.person_model or DEFAULT_MODEL)
        people = YoloPersonTracker(fps=fps, model_path=model, imgsz=tun.detector_imgsz,
                                   conf=tun.detector_conf, hidden_sec=tun.hidden_sec,
                                   min_age_sec=tun.min_track_age_sec, coast_sec=tun.coast_sec,
                                   with_reid=tun.with_reid, fixed_camera=tun.fixed_camera,
                                   detect_fps=tun.detect_fps,
                                   tracker_overrides={"new_track_thresh": tun.new_track_thresh})
    else:
        people = people_factory(fps)
    writer = FrameWriter(output_path, width, height, fps, audio_source=input_path, max_seconds=max_seconds)
    frames = _Prefetch(reader, total)
    output = _Output(writer, fps, total, realtime=live, on_frame=on_frame, on_progress=on_progress)
    checks = ThreadPoolExecutor(max_workers=CHECKS_IN_FLIGHT, thread_name_prefix="check")

    def new_status() -> PersonStatus:
        return PersonStatus(window=tun.vote_window, confirm=tun.confirm_checks)

    statuses: dict[int, PersonStatus] = {}
    first_seen: dict[int, int] = {}  # track id -> first frame on screen
    attempted: set[int] = set()  # tracks that had a check (even a failed one)
    timeline = []
    pending: deque[dict] = deque()  # frames tracked but not written yet, oldest first
    late: deque[dict] = deque()  # live mode: check frames already written, results not applied yet
    counts = {"worker_checks": 0, "failed_checks": 0, "failed_in_a_row": 0, "in_flight": 0, "checks_out": 0}
    last_error = None

    def display_for(tracked) -> list[WorkerResult]:
        return [statuses[p.id].display(p.id, p.box, too_small=p.box[3] - p.box[1] < min_check_h)
                for p in tracked if p.id in statuses]

    def write(rec: dict) -> None:
        """Hands a frame to the writer: with its check applied, or (live mode, check still
        out) with the statuses so far, keeping the check to apply when its results are in."""
        while late and late[0]["future"].done():
            apply(late.popleft())
        if live and rec["future"] is not None and (late or not rec["future"].done()):
            late.append(rec)  # results apply in check order, so this waits behind earlier ones
            output.put(rec["idx"], rec["frame"].copy(), display_for(rec["tracked"]))  # drawing marks the frame
        else:
            output.put(rec["idx"], rec["frame"], apply(rec))

    def apply(rec: dict) -> list[WorkerResult]:
        """Applies a frame's check results (waiting for them if needed) and records the check;
        returns the boxes to draw on that frame."""
        nonlocal last_error
        idx, frame, tracked, live_people, big, due = (rec["idx"], rec["frame"], rec["tracked"], rec["live"],
                                                      rec["big"], rec["due"])
        results: dict[int, WorkerResult] = {}
        if rec["future"] is not None:
            try:
                checked = rec["future"].result()  # AuthError / ModelError: every later check would fail too
            finally:
                counts["in_flight"] -= len(due)
                counts["checks_out"] -= 1
                if on_check:
                    on_check(idx, counts["in_flight"])
            for p, r in zip(due, checked):
                r.track_id = p.id
                results[p.id] = r
                counts["worker_checks"] += 1
                if r.check_failed:
                    counts["failed_checks"] += 1
                    last_error = r.error
                    continue
                statuses[p.id].add(r, idx)
            if all(r.check_failed for r in results.values()):
                counts["failed_in_a_row"] += 1
                log.warning("PPE check failed at frame %d (%s)", idx, last_error)
                if counts["failed_in_a_row"] >= tun.max_failed_checks:
                    raise RuntimeError(f"The PPE check failed on {counts['failed_in_a_row']} checks in a row, so the "
                                       f"run was stopped. Last error: {last_error}")
            else:
                counts["failed_in_a_row"] = 0

        display = display_for(tracked)
        is_check = rec["is_check"]
        arrival = bool(due) and not is_check
        if is_check or arrival:
            if is_check:
                # Regular check: everyone on screen. Too-far and too-new workers are marked as such.
                current = [results.get(p.id) or WorkerResult(box=p.box, track_id=p.id, too_small=p.id not in big,
                                                             pending=p.id in big)
                           for p in live_people]
            else:
                # Arrival check: only the newcomers, so they're in the report even if they
                # leave before the next regular check.
                current = [results[p.id] for p in due]
            entry = {
                "frame": idx,
                "time_sec": round(idx / fps, 2),
                "arrival": arrival,
                # Wall-clock time the check's results were recorded: when each violation was detected.
                "detected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "workers": [_worker_entry(r, statuses.get(r.track_id)) for r in current],
            }
            timeline.append(entry)
            if on_analysed:
                on_analysed(entry, frame, current, display)
            log.info("frame %d (%.1fs)%s: %d workers, %d with violations", idx, idx / fps,
                     " arrival" if arrival else "", len(current), sum(bool(r.violations) for r in current))
        return display

    idx = 0
    try:
        while (frame := frames.get()) is not None:
            tracked = people.update(frame)
            on_screen = [p for p in tracked if not getattr(p, "coasting", False)]
            for p in on_screen:
                statuses.setdefault(p.id, new_status())
                first_seen.setdefault(p.id, idx)

            def old_enough(p) -> bool:
                # People on screen from the start are established, not flicker.
                return first_seen[p.id] == 0 or idx - first_seen[p.id] >= min_age

            big = {p.id for p in on_screen if p.box[3] - p.box[1] >= min_check_h}
            checkable = [p for p in on_screen if p.id in big and old_enough(p)]
            is_check = idx % step == 0
            if is_check:
                due = checkable
            elif step - idx % step > arrival_cutoff:
                # Someone new is checked now rather than at the next check frame, unless that's close.
                # A failed attempt waits for the next check frame instead of retrying every frame.
                due = [p for p in checkable if p.id not in attempted
                       and not (tun.skip_edge_arrivals and _at_edge(p.box, width, height))]
            else:
                due = []
            if live and due and counts["checks_out"] >= CHECKS_IN_FLIGHT:
                # Moondream is behind: skip this check rather than queue it. A newcomer is
                # tried again on the next frame, everyone else at the next check frame.
                due, is_check = [], False

            future = None
            if due:
                rgb = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                future = checks.submit(analyzer.check_workers, rgb, [p.box for p in due],
                                       [p.box for p in on_screen])
                attempted.update(p.id for p in due)
                counts["in_flight"] += len(due)
                counts["checks_out"] += 1
                if on_check:
                    on_check(idx, counts["in_flight"])
            pending.append({"idx": idx, "frame": frame, "tracked": tracked, "live": on_screen, "big": big,
                            "due": due, "is_check": is_check, "future": future})
            # Write every frame whose check is done; wait only when too many frames are held back.
            while pending and (pending[0]["future"] is None or pending[0]["future"].done()
                               or len(pending) > max_buffered):
                write(pending.popleft())
            idx += 1
        while pending:
            write(pending.popleft())
        while late:
            apply(late.popleft())
        timeline.sort(key=lambda e: e["frame"])  # live mode applies some checks after later frames
        if counts["worker_checks"] and counts["failed_checks"] == counts["worker_checks"]:
            raise RuntimeError(f"Moondream analysis failed on every check. Last error: {last_error}")
        output.close()
        writer.close()
    except BaseException:
        output.abort()
        writer.abort()
        raise
    finally:
        checks.shutdown(wait=False, cancel_futures=True)
        frames.close()
        reader.close()
    worker_checks, failed_checks = counts["worker_checks"], counts["failed_checks"]

    report = {
        "input": str(input_path),
        "output": str(output_path),
        "fps": fps,
        "width": width,
        "height": height,
        "duration_sec": round(output.written / fps, 2),
        "analysis_interval_sec": cfg.analysis_interval_sec,
        "live": live,
        "frames_written": output.written,
        "frames_analysed": sum(not e["arrival"] for e in timeline),  # regular checks
        "arrival_checks": sum(e["arrival"] for e in timeline),
        "worker_checks": worker_checks,
        "failed_checks": failed_checks,
        "required_ppe": cfg.required_ppe,
        "moondream_endpoint": cfg.moondream_endpoint,
        "tuning": tun.model_dump(),
        "timeline": timeline,
    }
    report_path = output_path.with_suffix(".json")
    report_path.write_text(json.dumps(report, indent=2))
    log.info("Wrote %s and %s", output_path, report_path)
    return report


def _worker_entry(r: WorkerResult, status: PersonStatus | None) -> dict:
    """One worker in a timeline entry: the raw check, and their voted status after it."""
    entry = {"track_id": r.track_id, "box": list(r.box), "label": r.label,
             "violations": r.violations, "unverified": r.unverified, "too_small": r.too_small,
             "pending": r.pending, "failed": r.failed, "check_failed": r.check_failed}
    if status is not None:
        entry.update(confirmed=status.confirmed, shown=list(status.shown),
                     shown_unverified=list(status.unverified))
    return entry
