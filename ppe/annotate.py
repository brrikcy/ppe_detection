"""Drawing of worker boxes and PPE tags onto frames."""

from __future__ import annotations

import cv2
import numpy as np

from .analyzer import WorkerResult

GREEN = (40, 180, 40)  # BGR
RED = (30, 30, 220)
AMBER = (0, 160, 230)  # some item couldn't be seen at the last check
GREY = (140, 140, 140)  # too far away, or not checked yet
WHITE = (255, 255, 255)
FONT = cv2.FONT_HERSHEY_SIMPLEX


def color_of(r: WorkerResult):
    if r.too_small or r.pending:
        return GREY
    if r.violations:
        return RED
    return AMBER if r.unverified else GREEN


def draw_results(frame: np.ndarray, results: list[WorkerResult]) -> np.ndarray:
    h, w = frame.shape[:2]
    scale = max(0.45, min(1.0, h / 1080))
    thickness = max(1, int(round(2 * scale)))
    line_w = max(2, int(round(3 * scale)))

    for r in results:
        x1, y1, x2, y2 = r.box
        cv2.rectangle(frame, (x1, y1), (x2, y2), color_of(r), line_w)

    # Tags after all boxes so no box line crosses a tag; top-most workers first.
    # The summary bar counts as taken, so a tag at the top edge goes below it.
    placed: list[tuple[int, int, int, int]] = [_draw_summary(frame, results, scale, thickness)]
    for r in sorted(results, key=lambda r: r.box[1]):
        text = r.tag
        (tw, th), base = cv2.getTextSize(text, FONT, scale, thickness)
        pad = 4
        tag_w, tag_h = tw + 2 * pad, th + base + 2 * pad
        x1, y1 = r.box[0], r.box[1]
        tx = min(max(0, x1), max(0, w - tag_w))
        # Above the box if there's room, else just inside it; then step down past any tag in the way.
        ty = y1 - tag_h if y1 - tag_h >= 0 else y1
        for _ in range(len(placed) + 1):
            hit = next((p for p in placed if _overlaps((tx, ty, tx + tag_w, ty + tag_h), p)), None)
            if hit is None:
                break
            ty = hit[3] + 1
        ty = min(ty, h - tag_h)
        placed.append((tx, ty, tx + tag_w, ty + tag_h))
        cv2.rectangle(frame, (tx, ty), (tx + tag_w, ty + tag_h), color_of(r), -1)
        cv2.putText(frame, text, (tx + pad, ty + pad + th), FONT, scale, WHITE, thickness, cv2.LINE_AA)
    return frame


def _overlaps(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _draw_summary(frame, results, scale, thickness) -> tuple[int, int, int, int]:
    """Draws the counts bar at the top left; returns its rectangle."""
    bad = sum(bool(r.violations) for r in results)
    ok = sum(not r.violations and not r.too_small and not r.pending for r in results)
    text = f"Workers: {len(results)}  |  PPE OK: {ok}  |  Violations: {bad}"
    (tw, th), base = cv2.getTextSize(text, FONT, scale, thickness)
    bar = frame[: th + base + 20, : tw + 20]  # darken just the bar's area (a view into frame)
    bar[:] = (bar * 0.45).astype(frame.dtype)
    cv2.putText(frame, text, (10, 10 + th), FONT, scale, WHITE, thickness, cv2.LINE_AA)
    return 0, 0, tw + 20, th + base + 20
