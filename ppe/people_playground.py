"""Person detection on its own: find and track people in a video, with no PPE checks.

Used by the person detection playground to try detector and tracker settings. It follows the
same rules as a run (ppe.video) for who would be checked for PPE, so the numbers show what a
run with these settings would do, without calling Moondream.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from .media import FrameReader, FrameWriter
from .tuning import PERSON_FIELDS, Tuning

log = logging.getLogger(__name__)

GREEN = (40, 180, 40)  # BGR: would be checked for PPE
GREY = (140, 140, 140)  # on screen but too far or too new to be checked
AMBER = (0, 160, 230)  # missed by the detector this frame; last box kept
WHITE = (255, 255, 255)
FONT = cv2.FONT_HERSHEY_SIMPLEX
SHORT_TRACK_SEC = 1.0  # a person who is on screen for less than this is probably a false detection


class Cancelled(Exception):
    """The test was stopped before the video ended."""


class _Track:
    __slots__ = ("id", "first", "last", "frames", "score_sum", "max_height", "checkable_frames")

    def __init__(self, tid: int, idx: int):
        self.id, self.first, self.last = tid, idx, idx
        self.frames = self.checkable_frames = 0
        self.score_sum = 0.0
        self.max_height = 0


def draw_people(frame: np.ndarray, people, checkable: set[int]) -> np.ndarray:
    """Boxes with `#id score`; green for people a run would check, grey for those it wouldn't,
    thin amber for a box kept after a missed detection."""
    h, w = frame.shape[:2]
    scale = max(0.45, min(1.0, h / 1080))
    thickness = max(1, int(round(2 * scale)))
    line_w = max(2, int(round(3 * scale)))
    shown = [p for p in people if not getattr(p, "coasting", False)]
    for p in sorted(people, key=lambda p: p.box[1]):
        x1, y1, x2, y2 = p.box
        coasting = getattr(p, "coasting", False)
        color = AMBER if coasting else GREEN if p.id in checkable else GREY
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 1 if coasting else line_w)
        if coasting:
            continue
        text = f"#{p.id} {p.score:.2f}"
        (tw, th), base = cv2.getTextSize(text, FONT, scale * 0.8, thickness)
        tx = min(max(0, x1), max(0, w - tw - 8))
        ty = y1 - th - base - 6 if y1 - th - base - 6 >= 0 else y1
        cv2.rectangle(frame, (tx, ty), (tx + tw + 8, ty + th + base + 6), color, -1)
        cv2.putText(frame, text, (tx + 4, ty + th + 3), FONT, scale * 0.8, WHITE, thickness, cv2.LINE_AA)
    text = f"People: {len(shown)}  |  would be checked: {sum(p.id in checkable for p in shown)}"
    (tw, th), base = cv2.getTextSize(text, FONT, scale, thickness)
    bar = frame[: th + base + 20, : tw + 20]
    bar[:] = (bar * 0.45).astype(frame.dtype)
    cv2.putText(frame, text, (10, 10 + th), FONT, scale, WHITE, thickness, cv2.LINE_AA)
    return frame


def detect_people(
    input_path: Path,
    output_path: Path,
    tuning: Tuning,
    *,
    default_model: Path | None = None,
    people_factory: Callable[[float], object] | None = None,
    max_seconds: float | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    """Tracks people through the video with `tuning`'s person settings and writes the video with
    their boxes to `output_path`. Returns a report of who was found (see the keys below).

    `people_factory(fps)` replaces the YOLO tracker (for tests). `on_progress(done, total)` is
    called every few frames; `should_stop()` returning True raises Cancelled.
    """
    input_path, output_path = Path(input_path), Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    reader = FrameReader(input_path)
    fps, width, height, total = reader.fps, reader.width, reader.height, reader.frame_count
    limit = int(max_seconds * fps) if max_seconds else None
    if limit is not None:
        total = min(total, limit) if total > 0 else limit

    if people_factory is None:
        from .people import tracker_from_tuning

        people = tracker_from_tuning(tuning, fps, default_model)
    else:
        people = people_factory(fps)
    writer = FrameWriter(output_path, width, height, fps, max_seconds=max_seconds)

    min_check_h = tuning.min_check_height(height)
    min_age = int(round(tuning.min_check_age_sec * fps))
    tracks: dict[int, _Track] = {}
    first_seen: dict[int, int] = {}
    per_second: dict[int, list[int]] = {}  # second -> people on screen in each of its frames
    nobody = most = 0
    detect_time = 0.0
    idx = 0
    try:
        while limit is None or idx < limit:
            frame = reader.read()
            if frame is None:
                break
            if should_stop and should_stop():
                raise Cancelled()
            started = time.perf_counter()
            tracked = people.update(frame)
            detect_time += time.perf_counter() - started
            on_screen = [p for p in tracked if not getattr(p, "coasting", False)]
            checkable: set[int] = set()
            for p in on_screen:
                first_seen.setdefault(p.id, idx)
                t = tracks.setdefault(p.id, _Track(p.id, idx))
                height_px = p.box[3] - p.box[1]
                t.last, t.frames, t.score_sum = idx, t.frames + 1, t.score_sum + p.score
                t.max_height = max(t.max_height, height_px)
                # The rule a run applies before it checks someone for PPE (ppe.video).
                if height_px >= min_check_h and (first_seen[p.id] == 0 or idx - first_seen[p.id] >= min_age):
                    checkable.add(p.id)
                    t.checkable_frames += 1
            per_second.setdefault(int(idx / fps), []).append(len(on_screen))
            nobody += not on_screen
            most = max(most, len(on_screen))
            writer.write(draw_people(frame, tracked, checkable))
            idx += 1
            if on_progress and idx % 5 == 0:
                on_progress(idx, total)
        writer.close()
    except BaseException:
        writer.abort()
        raise
    finally:
        reader.close()
    if on_progress:
        on_progress(idx, max(total, idx))

    elapsed_frames = max(1, idx)
    short = [t for t in tracks.values() if (t.last - t.first + 1) / fps < SHORT_TRACK_SEC]
    return {
        "input": str(input_path),
        "frames": idx,
        "fps": round(fps, 3),
        "width": width,
        "height": height,
        "duration_sec": round(idx / fps, 2),
        "decoder": reader.decoder,
        "detect_seconds": round(detect_time, 2),  # time spent finding and tracking people
        "processing_fps": round(idx / detect_time, 1) if detect_time > 0 else None,
        "min_check_height_px": round(min_check_h, 1),
        "people": len(tracks),
        "would_be_checked": sum(t.checkable_frames > 0 for t in tracks.values()),
        "never_checked": sum(t.checkable_frames == 0 for t in tracks.values()),
        "short_tracks": len(short),
        "avg_per_frame": round(sum(sum(v) for v in per_second.values()) / elapsed_frames, 2),
        "max_per_frame": most,
        "frames_without_people": nobody,
        "timeline": [{"time_sec": s, "people": round(sum(v) / len(v), 2)} for s, v in sorted(per_second.items())],
        "tracks": [
            {"id": t.id, "first_sec": round(t.first / fps, 2), "last_sec": round(t.last / fps, 2),
             "frames": t.frames, "avg_score": round(t.score_sum / t.frames, 2), "max_height_px": t.max_height,
             "checked": t.checkable_frames > 0, "short": t in short}
            for t in sorted(tracks.values(), key=lambda t: t.first)
        ],
        "settings": {k: getattr(tuning, k) for k in PERSON_FIELDS},
    }
