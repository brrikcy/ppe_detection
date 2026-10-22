"""Drawing of worker boxes and PPE tags onto frames."""

from __future__ import annotations

import cv2
import numpy as np

from .analyzer import WorkerResult

GREEN = (40, 180, 40)  # BGR
RED = (30, 30, 220)
WHITE = (255, 255, 255)
FONT = cv2.FONT_HERSHEY_SIMPLEX


def draw_results(frame: np.ndarray, results: list[WorkerResult]) -> np.ndarray:
    h, w = frame.shape[:2]
    scale = max(0.45, min(1.0, h / 1080))
    thickness = max(1, int(round(2 * scale)))
    line_w = max(2, int(round(3 * scale)))

    for r in results:
        color = GREEN if r.ok else RED
        x1, y1, x2, y2 = r.box
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, line_w)

        text = f"#{r.track_id} {r.label}" if r.track_id is not None else r.label
        (tw, th), base = cv2.getTextSize(text, FONT, scale, thickness)
        pad = 4
        # Put the tag above the box; drop it inside if there's no room.
        ty = y1 - th - base - 2 * pad
        if ty < 0:
            ty = y1
        tx = min(max(0, x1), max(0, w - tw - 2 * pad))
        cv2.rectangle(frame, (tx, ty), (tx + tw + 2 * pad, ty + th + base + 2 * pad), color, -1)
        cv2.putText(frame, text, (tx + pad, ty + pad + th), FONT, scale, WHITE, thickness, cv2.LINE_AA)

    _draw_summary(frame, results, scale, thickness)
    return frame


def _draw_summary(frame, results, scale, thickness):
    ok = sum(r.ok for r in results)
    bad = len(results) - ok
    text = f"Workers: {len(results)}  |  PPE OK: {ok}  |  Violations: {bad}"
    (tw, th), base = cv2.getTextSize(text, FONT, scale, thickness)
    overlay = frame.copy()
    cv2.rectangle(overlay, (0, 0), (tw + 20, th + base + 20), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.55, frame, 0.45, 0, frame)
    cv2.putText(frame, text, (10, 10 + th), FONT, scale, WHITE, thickness, cv2.LINE_AA)
