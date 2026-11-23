"""Background execution of runs.

Runs execute one at a time on a single worker thread, which keeps Moondream's
rate limit manageable; extra submissions wait as "queued".
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import cv2

from ppe.analyzer import PPEAnalyzer, WorkerResult
from ppe.annotate import draw_results
from ppe.config import Config
from ppe.moondream_client import DEFAULT_ENDPOINT, AuthError, ModelError
from ppe.video import process_video

from ..schemas.runs import Progress, RunStatus
from .run_store import RunStore, replace_with_retry
from .summary import clean_frame_name, frame_name, person_photo_name, snapshot_name

log = logging.getLogger(__name__)

AnalyzerFactory = Callable[[Config], PPEAnalyzer]
PeopleFactory = Callable[[float], object]  # fps -> person tracker; None = YOLO (ppe.people)

PREVIEW_MAX_WIDTH = 1280
SNAPSHOT_MAX_HEIGHT = 320
PROGRESS_SAVE_INTERVAL_SEC = 0.5
PREVIEW_INTERVAL_SEC = 0.4  # live preview refresh while frames are written
# Live stream frames: ~70 KB each, so ~10 fps fits a 0.75 MB/s connection (207 KB at 1280 px, quality 75).
STREAM_MAX_WIDTH = 800
STREAM_JPEG_QUALITY = 60
STREAM_POLL_SEC = 1.0  # a stream with no new frame checks this often whether the run has ended


def default_analyzer_factory(config: Config) -> PPEAnalyzer:
    return PPEAnalyzer(config)


class LiveFeed:
    """The latest frame written by a running run, as JPEG, for the live stream.

    Frames are only encoded while someone is watching.
    """

    def __init__(self):
        self._cond = threading.Condition()
        self._jpeg: bytes | None = None
        self._seq = 0
        self._closed = False
        self.viewers = 0

    def push(self, img) -> None:
        h, w = img.shape[:2]
        if w > STREAM_MAX_WIDTH:
            img = cv2.resize(img, (STREAM_MAX_WIDTH, int(h * STREAM_MAX_WIDTH / w)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, STREAM_JPEG_QUALITY])
        if not ok:
            return
        with self._cond:
            self._jpeg, self._seq = buf.tobytes(), self._seq + 1
            self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    @contextmanager
    def watching(self):
        """Frames are encoded while at least one viewer is inside this block."""
        with self._cond:
            self.viewers += 1
        try:
            yield self
        finally:
            with self._cond:
                self.viewers -= 1

    def next_frame(self, seq: int) -> tuple[int, bytes | None] | None:
        """The newest frame after number `seq`, as (number, jpeg), skipping any in between;
        (seq, None) if none came within STREAM_POLL_SEC; None once the run has ended."""
        with self._cond:
            self._cond.wait_for(lambda: self._seq != seq or self._closed, timeout=STREAM_POLL_SEC)
            if self._closed:
                return None
            if self._seq == seq:
                return seq, None
            return self._seq, self._jpeg


class RunRunner:
    def __init__(self, store: RunStore, analyzer_factory: AnalyzerFactory = default_analyzer_factory,
                 people_factory: PeopleFactory | None = None, person_model: Path | None = None,
                 moondream_endpoint: str = DEFAULT_ENDPOINT):
        self.store = store
        self.analyzer_factory = analyzer_factory
        self.people_factory = people_factory
        self.person_model = person_model
        self.moondream_endpoint = moondream_endpoint
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="run")
        self._live: dict[str, list[dict]] = {}  # timeline so far for active runs
        self._feeds: dict[str, LiveFeed] = {}  # frames as they're written, for active runs
        self._live_lock = threading.Lock()

    def submit(self, run_id: str, api_key: str) -> None:
        # The key only lives in this closure for the duration of the run.
        self._executor.submit(self._execute, run_id, api_key)

    def live_timeline(self, run_id: str) -> list[dict] | None:
        with self._live_lock:
            entries = self._live.get(run_id)
            # Live mode can record a check after later ones; the summary expects frame order.
            return sorted(entries, key=lambda e: e["frame"]) if entries is not None else None

    def feed(self, run_id: str) -> LiveFeed | None:
        with self._live_lock:
            return self._feeds.get(run_id)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    # -- internals -----------------------------------------------------------
    def _execute(self, run_id: str, api_key: str) -> None:
        meta = self.store.get(run_id)
        paths = self.store.paths(run_id)
        opts = meta.options
        config = Config(
            api_key=api_key,
            required_ppe=opts.required_ppe,
            analysis_interval_sec=opts.analysis_interval_sec,
            person_model=str(self.person_model) if self.person_model else None,
            tuning=opts.tuning,
            moondream_endpoint=meta.moondream_endpoint or self.moondream_endpoint,
        )
        feed = LiveFeed()
        with self._live_lock:
            self._live[run_id] = []
            self._feeds[run_id] = feed
        progress = Progress(frames_total=meta.video.frame_count)
        self.store.update(run_id, status=RunStatus.running, progress=progress)

        last_save = 0.0

        def on_progress(done: int, total: int) -> None:
            nonlocal last_save
            progress.frames_done, progress.frames_total = done, total or progress.frames_total
            now = time.monotonic()
            if now - last_save >= PROGRESS_SAVE_INTERVAL_SEC or done == total:
                last_save = now
                self.store.update(run_id, progress=progress)

        best_area: dict[int, int] = {}

        def on_analysed(entry: dict, frame, results: list[WorkerResult], display: list[WorkerResult]) -> None:
            arrival = entry.get("arrival", False)
            if not arrival:
                progress.frames_analysed += 1  # counts regular checks, like the report
            try:
                self._save_people(paths.people, frame, results, best_area)
                self._save_snapshots(paths.snapshots, entry["frame"], frame, results)
                # The preview and frame images show the boxes as drawn in the video (voted status).
                # An arrival check only has the newcomers, so it doesn't replace the live preview.
                self._save_preview(paths.preview, frame, display,
                                   violation_copy=paths.frames / frame_name(entry["frame"]),
                                   keep_copy=any(not r.ok for r in results) or any(r.violations for r in display),
                                   update_preview=not arrival)
            except OSError:
                # e.g. Windows refuses to replace preview.jpg while the browser is fetching it.
                log.warning("Couldn't save preview/snapshots for frame %d", entry["frame"], exc_info=True)
            with self._live_lock:
                self._live[run_id].append(entry)

        last_preview = 0.0

        def on_frame(idx: int, annotated) -> None:
            # The live page shows the annotated video as it's written, not just one still per check.
            nonlocal last_preview
            if feed.viewers:
                feed.push(annotated)
            now = time.monotonic()
            if now - last_preview < PREVIEW_INTERVAL_SEC:
                return
            last_preview = now
            try:
                self._write_preview(paths.preview, annotated)
            except OSError:
                log.debug("Couldn't update the live preview", exc_info=True)

        def on_check(idx: int, workers: int) -> None:
            # Moondream can take a while; the live page says what it's waiting for.
            progress.checking = workers
            if workers:
                progress.frames_done = max(progress.frames_done, idx)
            self.store.update(run_id, progress=progress)

        analyzer = self.analyzer_factory(config)
        try:
            process_video(paths.input(), paths.output, analyzer, max_seconds=opts.max_seconds,
                          on_progress=on_progress, on_analysed=on_analysed,
                          on_start=lambda w, h: self.store.update(run_id, frame_size=[w, h]),
                          people_factory=self.people_factory, on_frame=on_frame, on_check=on_check,
                          live=opts.live)
            progress.frames_done = progress.frames_total = max(progress.frames_done, 1)
            self.store.update(run_id, status=RunStatus.completed, progress=progress,
                              finished_at=datetime.now(timezone.utc))
        except AuthError:
            self._fail(run_id, "The API key was rejected. Update it in Developer options and start a new run.")
        except ModelError:
            self._fail(run_id, f"The vision model endpoint doesn't accept model {opts.tuning.moondream_model!r}. "
                               "Change it in Developer options and start a new run.")
        except Exception as exc:
            log.exception("Run %s failed", run_id)
            self._fail(run_id, str(exc) or exc.__class__.__name__)
        finally:
            analyzer.close()
            feed.close()
            with self._live_lock:
                self._live.pop(run_id, None)
                self._feeds.pop(run_id, None)

    def _fail(self, run_id: str, message: str) -> None:
        self.store.update(run_id, status=RunStatus.failed, error=message,
                          finished_at=datetime.now(timezone.utc))

    @staticmethod
    def _save_preview(path, frame, results, violation_copy=None, keep_copy=False, update_preview=True) -> None:
        """Latest annotated frame for the live view; also kept under frames/ when `keep_copy`,
        together with the frame without boxes, so the viewer can draw just one worker's box."""
        img = draw_results(frame.copy(), results)
        h, w = img.shape[:2]
        size = (PREVIEW_MAX_WIDTH, int(h * PREVIEW_MAX_WIDTH / w)) if w > PREVIEW_MAX_WIDTH else None
        if size:
            img = cv2.resize(img, size)
        if violation_copy is not None and keep_copy:
            violation_copy.parent.mkdir(exist_ok=True)
            cv2.imwrite(str(violation_copy), img, [cv2.IMWRITE_JPEG_QUALITY, 85])
            clean = cv2.resize(frame, size, interpolation=cv2.INTER_AREA) if size else frame
            cv2.imwrite(str(violation_copy.with_name(clean_frame_name(violation_copy.name))), clean,
                        [cv2.IMWRITE_JPEG_QUALITY, 85])
        if update_preview:
            RunRunner._write_preview(path, img)

    @staticmethod
    def _write_preview(path, img) -> None:
        h, w = img.shape[:2]
        if w > PREVIEW_MAX_WIDTH:
            img = cv2.resize(img, (PREVIEW_MAX_WIDTH, int(h * PREVIEW_MAX_WIDTH / w)))
        tmp = path.with_name("preview.tmp.jpg")
        cv2.imwrite(str(tmp), img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        replace_with_retry(tmp, path)

    @staticmethod
    def _save_people(folder, frame, results: list[WorkerResult], best_area: dict[int, int]) -> None:
        """Keep the largest view of each tracked person as their photo."""
        h, w = frame.shape[:2]
        for r in results:
            if r.track_id is None:
                continue
            x1, y1, x2, y2 = r.box
            area = (x2 - x1) * (y2 - y1)
            if area <= best_area.get(r.track_id, 0):
                continue
            best_area[r.track_id] = area
            crop = frame[max(0, y1):min(h, y2), max(0, x1):min(w, x2)]
            if crop.size == 0:
                continue
            ch, cw = crop.shape[:2]
            if ch > SNAPSHOT_MAX_HEIGHT:
                crop = cv2.resize(crop, (max(1, int(cw * SNAPSHOT_MAX_HEIGHT / ch)), SNAPSHOT_MAX_HEIGHT))
            folder.mkdir(exist_ok=True)
            cv2.imwrite(str(folder / person_photo_name(r.track_id)), crop, [cv2.IMWRITE_JPEG_QUALITY, 85])

    @staticmethod
    def _save_snapshots(folder, frame_idx: int, frame, results: list[WorkerResult]) -> None:
        h, w = frame.shape[:2]
        # Every check that reached Moondream, so each person's profile can show all their crops.
        for i, r in enumerate(results):
            if r.too_small or r.pending or r.check_failed:
                continue
            x1, y1, x2, y2 = r.box
            pad_x, pad_y = int((x2 - x1) * 0.1), int((y2 - y1) * 0.1)
            crop = frame[max(0, y1 - pad_y):min(h, y2 + pad_y), max(0, x1 - pad_x):min(w, x2 + pad_x)]
            if crop.size == 0:
                continue
            ch, cw = crop.shape[:2]
            if ch > SNAPSHOT_MAX_HEIGHT:
                crop = cv2.resize(crop, (max(1, int(cw * SNAPSHOT_MAX_HEIGHT / ch)), SNAPSHOT_MAX_HEIGHT))
            cv2.imwrite(str(folder / snapshot_name(frame_idx, i)), crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
