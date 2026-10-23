"""Video pipeline: track people on every frame -> check PPE on a schedule -> annotate -> encode."""

from __future__ import annotations

import json
import logging
from collections import deque
from concurrent.futures import ThreadPoolExecutor
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


def _at_edge(box, width: int, height: int) -> bool:
    x1, y1, x2, y2 = box
    return x1 <= EDGE_PX or y1 <= EDGE_PX or x2 >= width - EDGE_PX or y2 >= height - EDGE_PX


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

    Tracking doesn't wait for Moondream: while a check frame is with Moondream, the next
    frames are read and tracked, and held back (up to BUFFER_BYTES) until the check's results
    are in. Frames are then drawn and written in order with the statuses as of that frame, so
    the output is the same as checking each frame before moving on.
    """
    input_path, output_path = Path(input_path), Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    reader = FrameReader(input_path)
    fps, width, height, total = reader.fps, reader.width, reader.height, reader.frame_count
    if max_seconds:
        total = min(total, int(max_seconds * fps)) if total > 0 else int(max_seconds * fps)

    cfg = analyzer.config
    tun = cfg.tuning
    step = max(1, int(round(cfg.analysis_interval_sec * fps)))
    arrival_cutoff = min(step // 2, max(1, int(round(ARRIVAL_CUTOFF_SEC * fps))))
    min_check_h = tun.min_check_height(height)
    min_age = int(round(tun.min_check_age_sec * fps))
    max_buffered = max(int(fps), BUFFER_BYTES // max(1, width * height * 3))
    log.info("%s: %dx%d @ %.1ffps, %d frames, checking every %d frames (%s decoding)",
             input_path.name, width, height, fps, total, step, reader.decoder)

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
    checks = ThreadPoolExecutor(max_workers=CHECKS_IN_FLIGHT, thread_name_prefix="check")

    def new_status() -> PersonStatus:
        return PersonStatus(window=tun.vote_window, confirm=tun.confirm_checks)

    statuses: dict[int, PersonStatus] = {}
    first_seen: dict[int, int] = {}  # track id -> first frame on screen
    attempted: set[int] = set()  # tracks that had a check (even a failed one)
    timeline = []
    pending: deque[dict] = deque()  # frames tracked but not written yet, oldest first
    counts = {"worker_checks": 0, "failed_checks": 0, "failed_in_a_row": 0, "in_flight": 0, "written": 0}
    last_error = None

    def finish(rec: dict) -> None:
        """Applies a frame's check results (waiting for them if needed), then draws and writes it."""
        nonlocal last_error
        idx, frame, tracked, live, big, due = rec["idx"], rec["frame"], rec["tracked"], rec["live"], rec["big"], rec["due"]
        results: dict[int, WorkerResult] = {}
        if rec["future"] is not None:
            try:
                checked = rec["future"].result()  # AuthError / ModelError: every later check would fail too
            finally:
                counts["in_flight"] -= len(due)
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
                    raise RuntimeError(f"Moondream failed on {counts['failed_in_a_row']} checks in a row, so the "
                                       f"run was stopped. Last error: {last_error}")
            else:
                counts["failed_in_a_row"] = 0

        display = [statuses[p.id].display(p.id, p.box, too_small=p.box[3] - p.box[1] < min_check_h)
                   for p in tracked if p.id in statuses]
        is_check = rec["is_check"]
        arrival = bool(due) and not is_check
        if is_check or arrival:
            if is_check:
                # Regular check: everyone on screen. Too-far and too-new workers are marked as such.
                current = [results.get(p.id) or WorkerResult(box=p.box, track_id=p.id, too_small=p.id not in big,
                                                             pending=p.id in big)
                           for p in live]
            else:
                # Arrival check: only the newcomers, so they're in the report even if they
                # leave before the next regular check.
                current = [results[p.id] for p in due]
            entry = {
                "frame": idx,
                "time_sec": round(idx / fps, 2),
                "arrival": arrival,
                "workers": [_worker_entry(r, statuses.get(r.track_id)) for r in current],
            }
            timeline.append(entry)
            if on_analysed:
                on_analysed(entry, frame, current, display)
            log.info("frame %d (%.1fs)%s: %d workers, %d with violations", idx, idx / fps,
                     " arrival" if arrival else "", len(current), sum(bool(r.violations) for r in current))

        annotated = draw_results(frame, display)
        writer.write(annotated)
        if on_frame:
            on_frame(idx, annotated)
        counts["written"] = done = idx + 1
        if on_progress and (done % 10 == 0 or done == total):
            on_progress(done, total)

    idx = 0
    try:
        while True:
            if total > 0 and idx >= total:
                break
            frame = reader.read()
            if frame is None:
                break
            tracked = people.update(frame)
            live = [p for p in tracked if not getattr(p, "coasting", False)]
            for p in live:
                statuses.setdefault(p.id, new_status())
                first_seen.setdefault(p.id, idx)

            def old_enough(p) -> bool:
                # People on screen from the start are established, not flicker.
                return first_seen[p.id] == 0 or idx - first_seen[p.id] >= min_age

            big = {p.id for p in live if p.box[3] - p.box[1] >= min_check_h}
            checkable = [p for p in live if p.id in big and old_enough(p)]
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

            future = None
            if due:
                rgb = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                future = checks.submit(analyzer.check_workers, rgb, [p.box for p in due], [p.box for p in live])
                attempted.update(p.id for p in due)
                counts["in_flight"] += len(due)
                if on_check:
                    on_check(idx, counts["in_flight"])
            pending.append({"idx": idx, "frame": frame, "tracked": tracked, "live": live, "big": big,
                            "due": due, "is_check": is_check, "future": future})
            # Write every frame whose check is done; wait only when too many frames are held back.
            while pending and (pending[0]["future"] is None or pending[0]["future"].done()
                               or len(pending) > max_buffered):
                finish(pending.popleft())
            idx += 1
        while pending:
            finish(pending.popleft())
        if counts["worker_checks"] and counts["failed_checks"] == counts["worker_checks"]:
            raise RuntimeError(f"Moondream analysis failed on every check. Last error: {last_error}")
        writer.close()
    except BaseException:
        writer.abort()
        raise
    finally:
        checks.shutdown(wait=False, cancel_futures=True)
        reader.close()
    worker_checks, failed_checks = counts["worker_checks"], counts["failed_checks"]

    report = {
        "input": str(input_path),
        "output": str(output_path),
        "fps": fps,
        "width": width,
        "height": height,
        "duration_sec": round(counts["written"] / fps, 2),
        "analysis_interval_sec": cfg.analysis_interval_sec,
        "frames_written": counts["written"],
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
