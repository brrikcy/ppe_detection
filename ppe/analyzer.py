"""PPE checks on tracked workers using Moondream.

Workers are found and tracked locally (ppe.people). For each worker due a check:
  1. The worker is cropped and asked one yes/no question per required PPE item
     (or one question for all items; prompts and the rules below are tunable, see ppe.tuning).
  2. A "no" answer is confirmed with `detect` on the crop. A detected box overrules
     the "no" only if it is on this worker (mostly inside the item's region of their
     box, and not on a neighbour), has a plausible size for the item, and Moondream
     confirms what it is when asked about that box alone. Either way, if the body part
     the item belongs on (e.g. the head) isn't visible, the item is unverified instead.
     Body parts are looked for with detect, except feet: Moondream is asked about those.
  3. A "yes" for a worker who overlaps a neighbour may be about the neighbour, so it is
     confirmed the same way; if it can't be, the item is unverified.
  4. Confirmed missing items become the violation list for that worker. An item whose
     calls failed is recorded as failed; the other items still count.

Workers too small to judge are not checked.
"""

from __future__ import annotations

import logging
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from PIL import Image

from .config import PPE_CATALOG, Config
from .moondream_client import AuthError, ModelError
from .tuning import ItemTuning

log = logging.getLogger(__name__)

VERIFY_PADDING = 0.3  # context around a detected box when asking what it is


@dataclass
class WorkerResult:
    box: tuple[int, int, int, int]  # x1, y1, x2, y2 in pixels
    violations: list[str] = field(default_factory=list)  # e.g. ["No Helmet", "No Vest"]
    unverified: list[str] = field(default_factory=list)  # items that couldn't be seen
    track_id: int | None = None  # person id, stable across frames (from ppe.people)
    too_small: bool = False  # too far away to check; no PPE verdict
    pending: bool = False  # tracked but no verdict yet
    failed: list[str] = field(default_factory=list)  # items whose Moondream calls failed
    check_failed: bool = False  # every item failed: this check says nothing about the worker
    error: str | None = None  # the first failure's message
    last_status: str | None = None  # too far now: their status from before (video display only)

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def label(self) -> str:
        if self.too_small:
            return "Too far to check"
        if self.check_failed:
            return "Check failed"
        if self.pending:
            return "Not checked yet"
        if self.ok:
            return "PPE OK"
        return f"PPE Violation ({len(self.violations)}) - {', '.join(self.violations)}"

    @property
    def tag(self) -> str:
        """Short text for the box in the video, e.g. "#3 Missing: Helmet, Vest"."""
        if self.too_small:
            text = f"Too far (was {self.last_status})" if self.last_status else "Too far"
        elif self.pending:
            text = "Checking..."
        elif self.violations:
            text = "Missing: " + ", ".join(v.removeprefix("No ") for v in self.violations)
        elif self.unverified:
            text = "OK? " + ", ".join(item_name(i) for i in self.unverified) + " unseen"
        else:
            text = "PPE OK"
        return f"#{self.track_id} {text}" if self.track_id is not None else text


def item_name(item: str) -> str:
    """ "helmet" -> "Helmet" (the catalog label without "No ")."""
    # Old runs may name an item that is no longer offered (mask).
    return PPE_CATALOG[item].label.removeprefix("No ") if item in PPE_CATALOG else item.capitalize()


def _with_retries(fn, *args, attempts: int = 6, **kwargs):
    """Calls fn, retrying failures with exponential back-off; a rejected API key is not retried."""
    delay = 1.0
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except (AuthError, ModelError):
            raise  # retrying won't fix a bad key or an unknown model
        except Exception as exc:  # SDK raises generic exceptions for HTTP errors
            if i == attempts - 1:
                raise
            log.warning("Moondream call failed (%s); retrying in %.0fs", exc, delay)
            time.sleep(delay + random.uniform(0, 0.5))
            delay *= 2


_YES = re.compile(r"^\W*(yes|yeah|yep)\b", re.I)
_NO = re.compile(r"^\W*(no|nope)\b(?!\s*(idea|way to tell)\b)", re.I)


def _is_yes(answer: str) -> bool | None:
    """True for "Yes."/"yes, ...", False for "No"/"no, ...", None for anything else.

    Whole words only, so "Not sure", "Not visible" and "None" are unclear, not "no".
    """
    if _YES.match(answer):
        return True
    if _NO.match(answer):
        return False
    return None


def _parse_multi(answer: str, items: list[str]) -> dict[str, str]:
    """Reads "helmet: yes\\nvest: no" style replies; items it can't find are left out."""
    out = {}
    for item in items:
        m = re.search(rf"\b{re.escape(item)}s?\b\W*[:\-=]?\s*(\w+)", answer, re.I)
        if m:
            out[item] = m.group(1)
    return out


def overlap_share(box, others) -> float:
    """Share of `box` covered by the other boxes (each overlap counted separately, capped at 1)."""
    x1, y1, x2, y2 = box
    area = max(1, (x2 - x1) * (y2 - y1))
    covered = 0
    for o in others:
        w = min(x2, o[2]) - max(x1, o[0])
        h = min(y2, o[3]) - max(y1, o[1])
        if w > 0 and h > 0:
            covered += w * h
    return min(1.0, covered / area)


class PPEAnalyzer:
    def __init__(self, config: Config, model=None):
        self.config = config
        if model is None:
            from .moondream_client import MoondreamCloud

            model = MoondreamCloud(api_key=config.api_key, endpoint=config.moondream_endpoint,
                                   model=config.tuning.moondream_model)
        self.model = model
        self.tuning = config.tuning
        self._pool = ThreadPoolExecutor(max_workers=self.tuning.parallel_requests)

    def _call(self, fn, *args):
        return _with_retries(fn, *args, attempts=self.tuning.retry_attempts)

    # -- PPE check -----------------------------------------------------------
    def _crop_box(self, image: Image.Image, box) -> tuple[float, float, float, float]:
        w, h = image.size
        x1, y1, x2, y2 = box
        px = (x2 - x1) * self.tuning.crop_padding
        py = (y2 - y1) * self.tuning.crop_padding
        return max(0, x1 - px), max(0, y1 - py), min(w, x2 + px), min(h, y2 + py)

    def _judge_box(self, frame_box, item: str | None, box, others, region, shape: bool) -> str:
        """Why a detected box (frame pixels) does or doesn't count for this worker: "ok" or a reason."""
        ox1, oy1, ox2, oy2 = frame_box
        centre = ((ox1 + ox2) / 2, (oy1 + oy2) / 2)
        if _owner(centre, box, others, region) != box:
            return "other worker" if _owner(centre, box, others, (-0.5, 1.5)) not in (None, box) else "outside region"
        bx1, by1, bx2, by2 = box
        bw, bh = bx2 - bx1, by2 - by1
        if shape:
            # Most of the detected box must be in the item's part of this worker, not just its centre.
            rx1, rx2 = bx1 - 0.1 * bw, bx2 + 0.1 * bw
            ry1, ry2 = by1 + region[0] * bh, by1 + region[1] * bh
            inside = max(0, min(ox2, rx2) - max(ox1, rx1)) * max(0, min(oy2, ry2) - max(oy1, ry1))
            if inside < self.tuning.min_region_overlap * max(1e-6, (ox2 - ox1) * (oy2 - oy1)):
                return "outside region"
            spec = self.tuning.items[item]
            wr, hr = (ox2 - ox1) / max(1, bw), (oy2 - oy1) / max(1, bh)
            if not (spec.min_width <= wr <= spec.max_width and spec.min_height <= hr <= spec.max_height):
                return "wrong size"
        return "ok"

    def _verify(self, image: Image.Image, frame_box, item: str, trace: list | None) -> bool:
        """Asks Moondream whether the detected box really is the item."""
        x1, y1, x2, y2 = frame_box
        px, py = (x2 - x1) * VERIFY_PADDING, (y2 - y1) * VERIFY_PADDING
        w, h = image.size
        crop = image.crop((max(0, x1 - px), max(0, y1 - py), min(w, x2 + px), min(h, y2 + py)))
        question = self.tuning.verify_question_for(item)
        answer = self._call(self.model.query, crop, question).get("answer", "")
        verdict = _is_yes(answer) is True
        if trace is not None:
            trace.append({"step": "verify", "question": question, "answer": answer, "confirmed": verdict})
        return verdict

    def _found_on_worker(self, image: Image.Image, crop: Image.Image, crop_box, term: str, box, others,
                         region: tuple[float, float], item: str | None = None,
                         trace: list | None = None) -> bool:
        """True if detect() finds `term` on this worker rather than elsewhere in the crop.

        With `item` set (the PPE item itself, not a body part), a box must also be mostly in
        the item's region, have a plausible size and, if enabled, be confirmed by Moondream.
        """
        cx1, cy1, cx2, cy2 = crop_box
        cw, ch = cx2 - cx1, cy2 - cy1
        objects = self._call(self.model.detect, crop, term).get("objects", [])
        found = False
        for o in objects:
            # Normalised crop coordinates -> frame pixels.
            fb = (cx1 + o["x_min"] * cw, cy1 + o["y_min"] * ch, cx1 + o["x_max"] * cw, cy1 + o["y_max"] * ch)
            step = {"step": "detect_object", "term": term, "box": o, "on_worker": False, "reason": "not needed"}
            if trace is not None:
                trace.append(step)
            if found:
                continue  # one is enough
            step["reason"] = self._judge_box(fb, item, box, others, region, shape=item is not None)
            if (step["reason"] == "ok" and item is not None and self.tuning.verify_detected_box
                    and not self._verify(image, fb, item, trace)):
                step["reason"] = "re-check said no"
            step["on_worker"] = found = step["reason"] == "ok"
        if trace is not None:
            trace.append({"step": "detect", "term": term, "found": len(objects), "on_worker": found})
        return found

    def _item_status(self, image: Image.Image, crop: Image.Image, crop_box, item: str, box, others,
                     trace: list | None = None, answer: str | None = None) -> str:
        """Returns "present", "missing" or "unverified". If `trace` is a list, each step is appended.

        `answer` is this item's reply when it was already asked as part of one question for all items.
        """
        t, spec = self.tuning, self.tuning.items[item]
        if answer is None:
            question = t.question_for(item)
            answer = self._call(self.model.query, crop, question).get("answer", "")
        else:
            question = None  # asked together with the other items
        verdict = _is_yes(answer)
        if trace is not None:
            trace.append({"step": "query", "question": question, "answer": answer,
                          "read_as": {True: "yes", False: "no", None: "unclear"}[verdict]})
        if verdict is None:
            log.debug("Unclear answer for %s: %r - treating as %s", item, answer, t.unclear_answer)
            return t.unclear_answer
        if verdict:
            share = overlap_share(box, others)
            if not t.confirm_yes_on_overlap or share < t.overlap_threshold:
                return "present"
            # The crop shows a neighbour too, and the "yes" may be about them.
            if trace is not None:
                trace.append({"step": "overlap", "share": round(share, 2)})
            if self._found_on_worker(image, crop, crop_box, spec.detect_term, box, others, spec.region, item, trace):
                return "present"
            return "unverified"
        # The query model says "no" too eagerly on occluded or cut-off workers,
        # so confirm before flagging a violation.
        if t.confirm_no_with_detect and self._found_on_worker(
                image, crop, crop_box, spec.detect_term, box, others, spec.region, item, trace):
            return "present"
        # Checked even without the detect above: a "no" about hidden feet is no violation.
        if t.check_body_part and not self._body_part_visible(image, crop, crop_box, spec, box, others, trace):
            return "unverified"
        return "missing"

    def _body_part_visible(self, image: Image.Image, crop: Image.Image, crop_box, spec: ItemTuning, box, others,
                           trace: list | None) -> bool:
        """Whether the body part the item belongs on can be seen; True if the item names none."""
        if spec.body_part_question:
            answer = self._call(self.model.query, crop, spec.body_part_question).get("answer", "")
            visible = _is_yes(answer) is True
            if trace is not None:
                trace.append({"step": "body_part", "question": spec.body_part_question, "answer": answer,
                              "visible": visible})
            return visible
        if spec.body_part:
            return self._found_on_worker(image, crop, crop_box, spec.body_part, box, others, spec.region, None, trace)
        return True

    def explain_item(self, image: Image.Image, item: str, box=None, others=()) -> dict:
        """Check one PPE item and return every step (for tuning).

        Without `box`, the whole image is the worker. With `box` (and the other workers'
        boxes), it is checked exactly as in a run: same crop, padding and neighbours.
        """
        if box is None:
            box = crop_box = (0, 0, *image.size)
            crop = image
        else:
            crop_box = self._crop_box(image, box)
            crop = image.crop(crop_box)
        trace: list = []
        status = self._item_status(image, crop, crop_box, item, tuple(box), [tuple(o) for o in others], trace)
        return {"item": item, "status": status, "steps": trace, "crop": crop}

    def _answers_together(self, crop: Image.Image, items: list[str]) -> dict[str, str]:
        """One question for all items; returns the replies it could read (others are asked one by one)."""
        try:
            reply = self._call(self.model.query, crop, self.tuning.multi_question_for(items)).get("answer", "")
        except (AuthError, ModelError):
            raise
        except Exception:
            log.warning("Combined PPE question failed; asking item by item", exc_info=True)
            return {}
        return _parse_multi(reply, items)

    def check_worker(self, image: Image.Image, box, others=()) -> WorkerResult:
        """`others` are the other workers' boxes in the frame, so their PPE isn't credited to this one."""
        if box[3] - box[1] < self.tuning.min_check_height(image.size[1]):
            return WorkerResult(box=box, too_small=True)
        crop_box = self._crop_box(image, box)
        crop = image.crop(crop_box)
        items = list(self.config.required_ppe)
        answers = self._answers_together(crop, items) if self.tuning.one_query_per_worker and len(items) > 1 else {}
        futures = {
            item: self._pool.submit(self._item_status, image, crop, crop_box, item, box, others,
                                    None, answers.get(item))
            for item in items
        }
        statuses, failed, error = {}, [], None
        for item, f in futures.items():
            try:
                statuses[item] = f.result()
            except (AuthError, ModelError):
                raise  # every later check would fail the same way
            except Exception as exc:
                log.warning("PPE check of %s failed: %s", item, exc)
                failed.append(item)
                error = error or str(exc) or exc.__class__.__name__
        return WorkerResult(
            box=box,
            violations=[PPE_CATALOG[i].label for i, st in statuses.items() if st == "missing"],
            unverified=[i for i, st in statuses.items() if st == "unverified"],
            failed=failed,
            check_failed=bool(items) and len(failed) == len(items),
            error=error,
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
