"""Per-frame PPE analysis using Moondream.

For each analysed frame:
  1. `detect(frame, "person")` finds every worker.
  2. Each worker is cropped and asked one yes/no question per required PPE item.
  3. Missing items become the violation list for that worker.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from PIL import Image

from .config import PPE_CATALOG, Config

log = logging.getLogger(__name__)


@dataclass
class WorkerResult:
    box: tuple[int, int, int, int]  # x1, y1, x2, y2 in pixels
    violations: list[str] = field(default_factory=list)  # e.g. ["No Helmet", "No Vest"]

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def label(self) -> str:
        if self.ok:
            return "PPE OK"
        return f"PPE Violation ({len(self.violations)}) - {', '.join(self.violations)}"


def _with_retries(fn, *args, attempts: int = 4, **kwargs):
    delay = 1.0
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # SDK raises generic exceptions for HTTP errors
            if i == attempts - 1:
                raise
            log.warning("Moondream call failed (%s); retrying in %.0fs", exc, delay)
            time.sleep(delay)
            delay *= 2


def _is_yes(answer: str) -> bool | None:
    a = answer.strip().lower()
    if a.startswith("yes"):
        return True
    if a.startswith("no"):
        return False
    return None


class PPEAnalyzer:
    def __init__(self, config: Config, model=None):
        self.config = config
        if model is None:
            from .moondream_client import MoondreamCloud

            model = MoondreamCloud(api_key=config.api_key)
        self.model = model
        self._pool = ThreadPoolExecutor(max_workers=config.max_workers)

    # -- detection -----------------------------------------------------------
    def detect_people(self, image: Image.Image) -> list[tuple[int, int, int, int]]:
        w, h = image.size
        objects = _with_retries(self.model.detect, image, "person").get("objects", [])
        boxes = []
        for o in objects:
            coords = [o["x_min"], o["y_min"], o["x_max"], o["y_max"]]
            # Moondream returns normalised [0, 1] coordinates.
            if max(coords) <= 1.0:
                coords = [coords[0] * w, coords[1] * h, coords[2] * w, coords[3] * h]
            x1, y1, x2, y2 = (int(round(c)) for c in coords)
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(w, x2), min(h, y2)
            if y2 - y1 < self.config.min_person_height_px or x2 <= x1:
                continue
            boxes.append((x1, y1, x2, y2))
        return boxes

    # -- PPE check -----------------------------------------------------------
    def _crop(self, image: Image.Image, box) -> Image.Image:
        w, h = image.size
        x1, y1, x2, y2 = box
        px = (x2 - x1) * self.config.crop_padding
        py = (y2 - y1) * self.config.crop_padding
        return image.crop(
            (max(0, x1 - px), max(0, y1 - py), min(w, x2 + px), min(h, y2 + py))
        )

    def _has_item(self, crop: Image.Image, item: str) -> bool:
        description = PPE_CATALOG[item][1]
        question = (
            f"Focus on the main person in the centre of this image. "
            f"Is this person wearing {description}? Answer only yes or no."
        )
        answer = _with_retries(self.model.query, crop, question).get("answer", "")
        verdict = _is_yes(answer)
        if verdict is None:
            log.debug("Unclear answer for %s: %r - treating as present", item, answer)
            # Benefit of the doubt: don't flag a violation on an ambiguous answer.
            return True
        return verdict

    def check_worker(self, image: Image.Image, box) -> WorkerResult:
        crop = self._crop(image, box)
        futures = {
            item: self._pool.submit(self._has_item, crop, item)
            for item in self.config.required_ppe
        }
        violations = [PPE_CATALOG[item][0] for item, f in futures.items() if not f.result()]
        return WorkerResult(box=box, violations=violations)

    def analyze(self, image: Image.Image) -> list[WorkerResult]:
        boxes = self.detect_people(image)
        # Worker checks submit item queries to the shared pool, so run the
        # per-worker fan-out on a separate pool to avoid deadlock.
        with ThreadPoolExecutor(max_workers=max(1, len(boxes))) as outer:
            return list(outer.map(lambda b: self.check_worker(image, b), boxes))

    def close(self):
        self._pool.shutdown(wait=True)
