"""PPE checks on tracked workers using Moondream.

Workers are found and tracked locally (ppe.people). For each worker due a check:
  1. The worker is cropped and asked one yes/no question per required PPE item.
  2. A "no" answer is confirmed with `detect` on the crop: if the item is found
     on this worker (in the right part of their box, and not on a neighbour who
     overlaps the crop) it counts as present; if the body part it belongs on
     (e.g. the head) isn't visible, it's recorded as unverified instead.
  3. Confirmed missing items become the violation list for that worker.

Workers too small to judge are not checked.
"""

from __future__ import annotations

import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from PIL import Image

from .config import PPE_CATALOG, Config
from .moondream_client import AuthError

log = logging.getLogger(__name__)


@dataclass
class WorkerResult:
    box: tuple[int, int, int, int]  # x1, y1, x2, y2 in pixels
    violations: list[str] = field(default_factory=list)  # e.g. ["No Helmet", "No Vest"]
    unverified: list[str] = field(default_factory=list)  # items that couldn't be seen
    track_id: int | None = None  # person id, stable across frames (from ppe.people)
    too_small: bool = False  # too far away to check; no PPE verdict
    pending: bool = False  # tracked but not checked yet (video display only)

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def label(self) -> str:
        if self.too_small:
            return "Too far to check"
        if self.ok:
            return "PPE OK"
        return f"PPE Violation ({len(self.violations)}) - {', '.join(self.violations)}"

    @property
    def tag(self) -> str:
        """Short text for the box in the video, e.g. "#3 Missing: Helmet, Vest"."""
        if self.too_small:
            text = "Too far"
        elif self.pending:
            text = "Checking..."
        elif self.violations:
            text = "Missing: " + ", ".join(v.removeprefix("No ") for v in self.violations)
        elif self.unverified:
            text = "OK? " + ", ".join(PPE_CATALOG[i].label.removeprefix("No ") if i in PPE_CATALOG else i
                                      for i in self.unverified) + " unseen"
        else:
            text = "PPE OK"
        return f"#{self.track_id} {text}" if self.track_id is not None else text


def _with_retries(fn, *args, attempts: int = 6, **kwargs):
    delay = 1.0
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except AuthError:
            raise  # retrying won't fix a bad key
        except Exception as exc:  # SDK raises generic exceptions for HTTP errors
            if i == attempts - 1:
                raise
            log.warning("Moondream call failed (%s); retrying in %.0fs", exc, delay)
            time.sleep(delay + random.uniform(0, 0.5))
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

    # -- PPE check -----------------------------------------------------------
    def _crop_box(self, image: Image.Image, box) -> tuple[float, float, float, float]:
        w, h = image.size
        x1, y1, x2, y2 = box
        px = (x2 - x1) * self.config.crop_padding
        py = (y2 - y1) * self.config.crop_padding
        return max(0, x1 - px), max(0, y1 - py), min(w, x2 + px), min(h, y2 + py)

    def _found_on_worker(self, crop: Image.Image, crop_box, term: str, box, others,
                         region: tuple[float, float]) -> bool:
        """True if detect() finds `term` on this worker rather than elsewhere in the crop."""
        cx1, cy1, cx2, cy2 = crop_box
        cw, ch = cx2 - cx1, cy2 - cy1
        for o in _with_retries(self.model.detect, crop, term).get("objects", []):
            # Normalised crop coordinates -> frame pixels.
            px = cx1 + (o["x_min"] + o["x_max"]) / 2 * cw
            py = cy1 + (o["y_min"] + o["y_max"]) / 2 * ch
            if _owner((px, py), box, others, region) == box:
                return True
        return False

    def _item_status(self, crop: Image.Image, crop_box, item: str, box, others) -> str:
        """Returns "present", "missing" or "unverified"."""
        spec = PPE_CATALOG[item]
        question = (
            f"Focus on the main person in the centre of this image. "
            f"Is this person wearing {spec.question}? Answer only yes or no."
        )
        answer = _with_retries(self.model.query, crop, question).get("answer", "")
        verdict = _is_yes(answer)
        if verdict is None:
            log.debug("Unclear answer for %s: %r - treating as present", item, answer)
            # Benefit of the doubt: don't flag a violation on an ambiguous answer.
            return "present"
        if verdict:
            return "present"
        # The query model says "no" too eagerly on occluded or cut-off workers,
        # so confirm before flagging a violation.
        if self._found_on_worker(crop, crop_box, spec.detect_term, box, others, spec.region):
            return "present"
        if spec.body_part and not self._found_on_worker(crop, crop_box, spec.body_part, box, others,
                                                         spec.region):
            return "unverified"
        return "missing"

    def check_worker(self, image: Image.Image, box, others=()) -> WorkerResult:
        """`others` are the other workers' boxes in the frame, so their PPE isn't credited to this one."""
        if box[3] - box[1] < self.config.min_check_height_frac * image.size[1]:
            return WorkerResult(box=box, too_small=True)
        crop_box = self._crop_box(image, box)
        crop = image.crop(crop_box)
        futures = {
            item: self._pool.submit(self._item_status, crop, crop_box, item, box, others)
            for item in self.config.required_ppe
        }
        statuses = {item: f.result() for item, f in futures.items()}
        return WorkerResult(
            box=box,
            violations=[PPE_CATALOG[i].label for i, st in statuses.items() if st == "missing"],
            unverified=[i for i, st in statuses.items() if st == "unverified"],
        )

    def check_workers(self, image: Image.Image, boxes, all_boxes=None) -> list[WorkerResult]:
        """Check `boxes` in parallel. `all_boxes` (default: `boxes`) are everyone in the frame,
        so PPE on a neighbour who isn't being checked right now still isn't credited."""
        everyone = list(all_boxes if all_boxes is not None else boxes)
        if not boxes:
            return []
        # Worker checks submit item queries to the shared pool, so run the
        # per-worker fan-out on a separate pool to avoid deadlock.
        with ThreadPoolExecutor(max_workers=len(boxes)) as outer:
            return list(outer.map(
                lambda b: self.check_worker(image, b, [o for o in everyone if o != b]), boxes))

    def close(self):
        self._pool.shutdown(wait=True)


def _owner(point, box, others, region: tuple[float, float]):
    """The worker box `point` belongs to, given where on a worker the item sits; None if nobody's.

    When several workers overlap at the point, the one whose box centre is horizontally
    closest (relative to box width) wins, so a worker half-hidden behind another keeps
    their own vest.
    """
    px, py = point
    best, best_d = None, None
    for b in (box, *others):
        bw, bh = b[2] - b[0], b[3] - b[1]
        if bw <= 0 or bh <= 0:
            continue
        if not (b[0] - 0.1 * bw <= px <= b[2] + 0.1 * bw):
            continue
        if not (b[1] + region[0] * bh <= py <= b[1] + region[1] * bh):
            continue
        d = abs(px - (b[0] + b[2]) / 2) / bw
        if best_d is None or d < best_d:
            best, best_d = b, d
    return best

