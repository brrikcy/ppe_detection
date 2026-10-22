"""Person tracking across frames, so each worker keeps one ID for the whole video.

Moondream only sees one frame per check interval, so tracking has two halves:

* Between checks, every track's box is moved along with the image content using
  sparse optical flow (Lucas-Kanade on corner features inside the box). This keeps
  boxes on the worker in the output video and gives a good prediction of where the
  worker will be at the next check, even when the camera pans.
* At each check, new detections are matched to the predicted boxes by overlap
  (IoU) plus an appearance score (HSV colour histogram of the crop). Unmatched
  detections start new tracks; tracks that go unmatched for `max_missed` checks
  are dropped, so a worker briefly hidden behind scaffolding keeps their ID.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

Box = tuple[int, int, int, int]

FLOW_MAX_WIDTH = 960  # optical flow runs on a downscaled copy for speed


@dataclass
class Track:
    id: int
    box: Box
    hist: np.ndarray
    missed: int = 0  # consecutive checks without a matching detection
    visible: bool = True  # matched at the latest check


def iou(a: Box, b: Box) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def appearance(frame: np.ndarray, box: Box) -> np.ndarray:
    """Normalised hue/saturation histogram of the torso area (where vests and shirts are)."""
    x1, y1, x2, y2 = box
    h = y2 - y1
    crop = frame[y1 + h // 6: y1 + (2 * h) // 3, x1:x2]
    if crop.size == 0:
        crop = frame[y1:y2, x1:x2]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [16, 8], [0, 180, 0, 256])
    return cv2.normalize(hist, hist).flatten()


def similarity(h1: np.ndarray, h2: np.ndarray) -> float:
    return 1.0 - float(cv2.compareHist(h1, h2, cv2.HISTCMP_BHATTACHARYYA))


@dataclass
class PersonTracker:
    max_missed: int = 3
    min_iou: float = 0.15
    min_appearance: float = 0.6  # for matches that rely on appearance rather than overlap
    tracks: list[Track] = field(default_factory=list)
    _next_id: int = 1
    _prev_gray: np.ndarray | None = None
    _scale: float = 1.0

    # -- per frame ---------------------------------------------------------
    def _gray(self, frame: np.ndarray) -> np.ndarray:
        h, w = frame.shape[:2]
        self._scale = min(1.0, FLOW_MAX_WIDTH / w)
        small = cv2.resize(frame, (int(w * self._scale), int(h * self._scale))) if self._scale < 1 else frame
        return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

    def propagate(self, frame: np.ndarray) -> None:
        """Move every track's box with the image content since the previous frame."""
        gray = self._gray(frame)
        prev, self._prev_gray = self._prev_gray, gray
        if prev is None or prev.shape != gray.shape or not self.tracks:
            return
        s = self._scale
        H, W = frame.shape[:2]
        for t in self.tracks:
            x1, y1, x2, y2 = (int(v * s) for v in t.box)
            if x2 - x1 < 4 or y2 - y1 < 4:
                continue
            mask = np.zeros_like(prev)
            mask[y1:y2, x1:x2] = 255
            pts = cv2.goodFeaturesToTrack(prev, maxCorners=40, qualityLevel=0.01, minDistance=4, mask=mask)
            if pts is None or len(pts) < 3:
                continue
            new, status, _ = cv2.calcOpticalFlowPyrLK(prev, gray, pts, None, winSize=(15, 15), maxLevel=3)
            ok = status.flatten() == 1
            if ok.sum() < 3:
                continue
            dx, dy = np.median((new[ok] - pts[ok]).reshape(-1, 2), axis=0) / s
            bw, bh = t.box[2] - t.box[0], t.box[3] - t.box[1]
            nx1 = int(np.clip(t.box[0] + dx, 0, W - 1))
            ny1 = int(np.clip(t.box[1] + dy, 0, H - 1))
            t.box = (nx1, ny1, min(W, nx1 + bw), min(H, ny1 + bh))

    # -- per check ---------------------------------------------------------
    def update(self, frame: np.ndarray, detections: list[Box]) -> list[int]:
        """Assign a track id to each detection (same order). Call after propagate()."""
        hists = [appearance(frame, b) for b in detections]
        candidates = []
        for di, (box, hist) in enumerate(zip(detections, hists)):
            diag = np.hypot(box[2] - box[0], box[3] - box[1])
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            for ti, t in enumerate(self.tracks):
                o = iou(box, t.box)
                a = similarity(hist, t.hist)
                tcx, tcy = (t.box[0] + t.box[2]) / 2, (t.box[1] + t.box[3]) / 2
                near = np.hypot(cx - tcx, cy - tcy) < 0.75 * diag
                if o >= self.min_iou or (near and a >= self.min_appearance):
                    candidates.append((0.7 * o + 0.3 * a, di, ti))

        # Greedy assignment, best pairs first (few people per frame, so this is fine).
        ids: list[int | None] = [None] * len(detections)
        used_t: set[int] = set()
        for _, di, ti in sorted(candidates, reverse=True):
            if ids[di] is None and ti not in used_t:
                t = self.tracks[ti]
                ids[di] = t.id
                used_t.add(ti)
                t.box, t.missed, t.visible = detections[di], 0, True
                t.hist = 0.5 * t.hist + 0.5 * hists[di]  # adapt slowly to lighting/pose changes

        for ti, t in enumerate(self.tracks):
            if ti not in used_t:
                t.missed += 1
                t.visible = False
        self.tracks = [t for t in self.tracks if t.missed <= self.max_missed]

        for di, box in enumerate(detections):
            if ids[di] is None:
                t = Track(id=self._next_id, box=box, hist=hists[di])
                self._next_id += 1
                self.tracks.append(t)
                ids[di] = t.id
        return ids  # type: ignore[return-value]

    def box_of(self, track_id: int) -> Box | None:
        return next((t.box for t in self.tracks if t.id == track_id), None)
