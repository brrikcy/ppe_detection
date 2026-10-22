"""Per-frame person detection and tracking: YOLO11n finds people, BoT-SORT links them over time.

Runs locally on every frame, so boxes follow workers smoothly and appear or disappear
on the right frame. Moondream is only asked about PPE (see ppe.analyzer).

BoT-SORT predicts each worker's motion with a Kalman filter, compensates for camera
motion, and keeps low-confidence detections in a second matching pass, so a worker
who is partly hidden stays the same person. Lost tracks are kept for `hidden_sec`
before their ID is retired.
"""

from __future__ import annotations

import os

# Never pip-install anything or phone home from inside a run.
os.environ.setdefault("YOLO_AUTOINSTALL", "false")
os.environ.setdefault("YOLO_OFFLINE", "true")

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

Box = tuple[int, int, int, int]

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = PROJECT_ROOT / "models" / "yolo11n.pt"
MODEL_URL = "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.pt"
PERSON_CLASS = 0  # COCO "person"

_models: dict[str, object] = {}


def _load_model(path: Path):
    if not path.exists():
        raise RuntimeError(f"Person detection model not found at {path}. Download it with:\n"
                           f"  curl -L -o {path} {MODEL_URL}")
    key = str(path)
    if key not in _models:
        from ultralytics import YOLO

        _models[key] = YOLO(key)
    return _models[key]


@dataclass
class TrackedPerson:
    id: int
    box: Box  # smoothed, in frame pixels
    score: float


@dataclass
class YoloPersonTracker:
    fps: float
    model_path: Path = DEFAULT_MODEL
    imgsz: int = 640  # detector input size; 4K frames are scaled down to this
    conf: float = 0.1  # low on purpose: BoT-SORT uses weak detections to keep existing tracks
    hidden_sec: float = 8.0  # a lost worker keeps their ID this long
    min_age_sec: float = 0.3  # tracks younger than this are flicker; not shown or checked yet
    smoothing: float = 0.5  # weight of the new box when smoothing displayed boxes
    tracker_overrides: dict = field(default_factory=dict)

    def __post_init__(self):
        from ultralytics.trackers.bot_sort import BOTSORT
        from ultralytics.utils import YAML, IterableSimpleNamespace
        from ultralytics.utils.checks import check_yaml

        self._model = _load_model(Path(self.model_path))
        cfg = {
            **YAML.load(check_yaml("botsort.yaml")),
            "new_track_thresh": 0.5,  # only start tracks from confident detections
            **self.tracker_overrides,
            # Ultralytics counts this in frames assuming 30 fps; size it from the real fps.
            "track_buffer": max(1, int(round(self.hidden_sec * self.fps))),
        }
        self._tracker = BOTSORT(IterableSimpleNamespace(**cfg))
        self._age: dict[int, int] = {}
        self._smooth: dict[int, np.ndarray] = {}
        self._frame = 0
        self._from_start: set[int] = set()  # on screen from the first frame; not flicker

    def update(self, frame: np.ndarray) -> list[TrackedPerson]:
        """Detect and track people in one BGR frame; returns established tracks only."""
        result = self._model.predict(frame, classes=[PERSON_CLASS], conf=self.conf, imgsz=self.imgsz,
                                     verbose=False)[0]
        rows = self._tracker.update(result.boxes.cpu().numpy(), frame)
        h, w = frame.shape[:2]
        min_age = max(1, int(round(self.min_age_sec * self.fps)))
        people, seen = [], set()
        for x1, y1, x2, y2, tid, score, *_ in rows:
            tid = int(tid)
            seen.add(tid)
            self._age[tid] = self._age.get(tid, 0) + 1
            box = np.array([x1, y1, x2, y2], dtype=float)
            prev = self._smooth.get(tid)
            box = box if prev is None else self.smoothing * box + (1 - self.smoothing) * prev
            self._smooth[tid] = box
            if self._frame == 0:
                self._from_start.add(tid)
            if self._age[tid] < min_age and tid not in self._from_start:
                continue
            bx1, by1 = int(np.clip(box[0], 0, w - 1)), int(np.clip(box[1], 0, h - 1))
            bx2, by2 = int(np.clip(box[2], bx1 + 1, w)), int(np.clip(box[3], by1 + 1, h))
            people.append(TrackedPerson(id=tid, box=(bx1, by1, bx2, by2), score=float(score)))
        # Forget smoothing for tracks not seen this frame, so a returning worker snaps to their new box.
        for tid in list(self._smooth):
            if tid not in seen:
                del self._smooth[tid]
        self._frame += 1
        return sorted(people, key=lambda p: p.box[0])
