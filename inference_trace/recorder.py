"""Records everything one inference run does, for the flowchart.

TracingAnalyzer is the real PPEAnalyzer with every Moondream call captured: the image it
was shown (saved as a JPEG), the prompt, the raw response and how long it took, in order
with the analyzer's own decision steps. TraceRecorder collects those per worker check and,
from the pipeline's callbacks, each check frame (with and without boxes), every worker's
state and vote, and how long each person was tracked.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from pathlib import Path

import cv2
from PIL import Image

from app.services.summary import check_state
from ppe.analyzer import PPEAnalyzer
from ppe.annotate import draw_results

FRAME_MAX_WIDTH = 1280  # check frames as saved for the flowchart
CROP_MAX_SIDE = 640  # images sent to Moondream, as saved (the originals can be 4K crops)

_local = threading.local()  # the step list the current thread's Moondream calls go into


class TracingAnalyzer(PPEAnalyzer):
    """PPEAnalyzer that logs each Moondream call into the step list of the item being checked."""

    def __init__(self, config, recorder: "TraceRecorder", model=None):
        super().__init__(config, model=model)
        self.recorder = recorder
        self.trace_hook = recorder.worker_checked

    def _call(self, fn, *args):
        image, prompt = args[0], args[1]
        steps = getattr(_local, "steps", None)
        step = {"step": "call", "kind": getattr(fn, "__name__", "call"), "prompt": prompt}
        if steps is not None:
            step["image"] = self.recorder.save_image(image, "ask")
            step["image_size"] = list(image.size)
            steps.append(step)
        t0 = time.perf_counter()
        try:
            out = super()._call(fn, *args)
        except Exception as exc:
            step.update(error=str(exc) or exc.__class__.__name__, ms=round((time.perf_counter() - t0) * 1000))
            self.recorder.count_call(step)
            raise
        step.update(response=_jsonable(out), ms=round((time.perf_counter() - t0) * 1000))
        self.recorder.count_call(step)
        return out

    def _item_status(self, image, crop, crop_box, item, box, others, trace=None, answer=None):
        _local.steps = trace
        try:
            return super()._item_status(image, crop, crop_box, item, box, others, trace, answer)
        finally:
            _local.steps = None

    def _answers_together(self, crop, items, trace=None):
        _local.steps = trace
        try:
            return super()._answers_together(crop, items, trace)
        finally:
            _local.steps = None


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class _TrackRecorder:
    """Wraps the person tracker to note when each person ID is on screen."""

    def __init__(self, tracker, tracks: dict):
        self.tracker, self.tracks, self.frame = tracker, tracks, -1

    def update(self, frame):
        self.frame += 1
        people = self.tracker.update(frame)
        for p in people:
            t = self.tracks.setdefault(p.id, {"id": p.id, "first_frame": self.frame, "frames": 0, "coasting": 0})
            t["last_frame"] = self.frame
            t["frames"] += 1
            t["coasting"] += bool(getattr(p, "coasting", False))
        return people

    def __getattr__(self, name):  # anything else the pipeline asks the tracker
        return getattr(self.tracker, name)


class TraceRecorder:
    def __init__(self, folder: Path, vote_window: int):
        self.folder = folder
        self.images = folder / "images"
        self.images.mkdir(parents=True, exist_ok=True)
        self.vote_window = vote_window
        self._lock = threading.Lock()
        self._n = 0
        self._saved: dict[int, tuple[object, str]] = {}  # id(image) -> (image, file name)
        self._records: dict[int, tuple[object, dict]] = {}  # id(WorkerResult) -> (result, record)
        self._history: dict[int, list] = defaultdict(list)  # track id -> raw results of its checks
        self.checks: list[dict] = []
        self.tracks: dict[int, dict] = {}
        self.calls = {"query": 0, "detect": 0, "failed": 0, "ms": 0}

    # ---- images --------------------------------------------------------------------
    def _next_name(self, kind: str) -> str:
        with self._lock:
            self._n += 1
            return f"{kind}_{self._n:05d}.jpg"

    def save_image(self, image: Image.Image, kind: str) -> str:
        """Saves a PIL image once (the same crop is asked about several times) and returns its name."""
        with self._lock:
            hit = self._saved.get(id(image))
            if hit and hit[0] is image:
                return hit[1]
        name = self._next_name(kind)
        small = image.copy()
        small.thumbnail((CROP_MAX_SIDE, CROP_MAX_SIDE))
        small.convert("RGB").save(self.images / name, quality=85)
        with self._lock:
            self._saved[id(image)] = (image, name)
        return name

    def _save_frame(self, bgr, kind: str) -> tuple[str, float]:
        h, w = bgr.shape[:2]
        scale = min(1.0, FRAME_MAX_WIDTH / w)
        if scale < 1:
            bgr = cv2.resize(bgr, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
        name = self._next_name(kind)
        cv2.imwrite(str(self.images / name), bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return name, scale

    def count_call(self, step: dict) -> None:
        with self._lock:
            kind = step["kind"] if step["kind"] in ("query", "detect") else "query"
            self.calls[kind] += 1
            self.calls["failed"] += "error" in step
            self.calls["ms"] += step.get("ms", 0)

    # ---- pipeline hooks -------------------------------------------------------------
    def tracker(self, tracker):
        return _TrackRecorder(tracker, self.tracks)

    def worker_checked(self, result, record: dict) -> None:
        """PPEAnalyzer.trace_hook: one worker's check, with every item's steps."""
        x1, y1, x2, y2 = record["crop_box"]
        rec = {
            "crop": self.save_image(record["crop"], "crop"),
            "crop_box": [round(v, 1) for v in (x1, y1, x2, y2)],
            "crop_size": list(record["crop"].size),
            "others": [list(map(int, o)) for o in record["others"]],
            "together": record["together"],
            "items": record["items"],
        }
        with self._lock:
            self._records[id(result)] = (result, rec)

    def on_check(self, entry: dict, frame_bgr, results, display) -> None:
        """process_video's on_analysed: one check frame."""
        clean, scale = self._save_frame(frame_bgr, "frame")
        annotated, _ = self._save_frame(draw_results(frame_bgr.copy(), display), "annotated")
        drawn = {d.track_id: d for d in display}
        workers = []
        for w, r in zip(entry["workers"], results):
            with self._lock:
                _, rec = self._records.pop(id(r), (None, None))
            tid = w.get("track_id")
            state = check_state(w)
            if rec is not None and not w.get("check_failed"):
                self._history[tid].append({"check": len(self.checks), "violations": list(w["violations"]),
                                           "unverified": list(w.get("unverified", []))})
            d = drawn.get(tid)
            workers.append({
                **{k: v for k, v in w.items() if k != "label"},
                "box": [int(v) for v in w["box"]],
                "state": state,
                "checked": rec is not None,
                "trace": rec,
                "window": self._history[tid][-self.vote_window:] if rec is not None else [],
                "drawn": {"tag": d.tag, "color": _color(d)} if d else None,
            })
        self.checks.append({
            "index": len(self.checks),
            "frame": entry["frame"],
            "time_sec": entry["time_sec"],
            "arrival": entry.get("arrival", False),
            "detected_at": entry.get("detected_at"),
            "image": clean,
            "annotated": annotated,
            "scale": scale,
            "workers": workers,
        })
        with self._lock:
            self._saved.clear()  # crops of this check are saved; don't hold on to them


def _color(d) -> str:
    """The box colour drawn in the video for this status (see ppe.annotate)."""
    if d.too_small or d.pending:
        return "grey"
    if d.violations:
        return "red"
    if d.unverified:
        return "amber"
    return "green"
