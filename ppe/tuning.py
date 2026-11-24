"""Tunable settings for the pipeline: Moondream prompts, decision rules, request
handling and person tracking. Edited from the dashboard's Developer options;
each run keeps a copy of the settings it used.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .moondream_client import DEFAULT_MODEL

MODEL_SUFFIXES = (".pt", ".engine", ".onnx")  # person-detection weights Ultralytics can load

# Settings state 1: the initial prompts (a plain yes/no question, validated by the double-checks).
INITIAL_QUESTION_TEMPLATE = (
    "Focus on the main person in the centre of this image. "
    "Is this person wearing {item}? Answer only yes or no."
)
INITIAL_VERIFY_TEMPLATE = "Is this a {term}? Answer only yes or no."
INITIAL_MULTI_TEMPLATE = (
    "Focus on the main person in the centre of this image. "
    "For each line, answer yes or no:\n{items}\n"
    "Reply with one line per item, like helmet: yes"
)

# Settings state 2: the prompt asks about visibility first.
DEFAULT_QUESTION_TEMPLATE = (
    "Focus on the main person in the centre of this image. "
    "Step 1: can you clearly see {part}? "
    "Step 2: only if you can, is this person wearing {item}? "
    "Reply in exactly this form: visible: yes or no; wearing: yes, no or n/a."
)
DEFAULT_MULTI_TEMPLATE = (
    "Focus on the main person in the centre of this image. "
    "For each line, first say whether the body part named on it can be clearly seen, then, only if it can, "
    "whether this person is wearing the item:\n{items}\n"
    "Reply with one line per item in exactly this form, like helmet: visible: yes; wearing: no"
)


class ItemTuning(BaseModel):
    """How one PPE item is asked about and, for a worker who overlaps a neighbour, confirmed."""

    question: str = Field(min_length=1, max_length=300)  # fills {item} in the question template
    detect_term: str = Field(min_length=1, max_length=80)  # confirms a "yes" with detect() when workers overlap
    # Where the item is worn, as named in the prompt: fills {part} in the question template ("can you
    # clearly see the head?") and is shown on each line of the combined question. Wording only.
    part_name: str = Field("the part of the body it is worn on", min_length=1, max_length=80)
    # Settings state 1 only (the initial method): if the body part the item is worn on can't be found
    # on the worker (detect), the item is unverified instead of missing. Blank = no check.
    body_part: str | None = Field(None, max_length=80)
    # Asked about the crop instead of detecting body_part; anything but "yes" makes the item unverified.
    body_part_question: str | None = Field(None, max_length=300)
    # Where on the worker the item sits, as fractions of the person box height from the top.
    region_top: float = Field(0.0, ge=-0.5, le=1.5)
    region_bottom: float = Field(1.0, ge=-0.5, le=1.5)
    # Plausible size of a detected item, as a share of the worker box width / height.
    # A "vest" box the size of a helmet doesn't confirm a "yes". Minimums are loose: a
    # bent-over worker has a wide box, and an item at the crop edge is detected cut off.
    min_width: float = Field(0.0, ge=0, le=2)
    max_width: float = Field(2.0, ge=0, le=2)
    min_height: float = Field(0.0, ge=0, le=2)
    max_height: float = Field(2.0, ge=0, le=2)

    @field_validator("question", "detect_term", "part_name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v

    @field_validator("body_part", "body_part_question")
    @classmethod
    def _blank_is_none(cls, v: str | None) -> str | None:
        return (v or "").strip() or None

    @model_validator(mode="after")
    def _region_order(self) -> "ItemTuning":
        if self.region_bottom <= self.region_top:
            raise ValueError("region_bottom must be below region_top")
        if self.max_width <= self.min_width or self.max_height <= self.min_height:
            raise ValueError("each maximum size must be larger than its minimum")
        return self

    @property
    def region(self) -> tuple[float, float]:
        return self.region_top, self.region_bottom


DEFAULT_ITEMS: dict[str, ItemTuning] = {
    "helmet": ItemTuning(question="a hard hat / safety helmet on their head", detect_term="hard hat",
                         part_name="the head", region_top=-0.1, region_bottom=0.35,
                         min_width=0.03, max_width=0.9, min_height=0.02, max_height=0.3),
    "vest": ItemTuning(question="a high-visibility safety vest or hi-vis jacket",
                       detect_term="high visibility vest", part_name="the upper body", region_top=0.1, region_bottom=0.8,
                       min_width=0.2, max_width=1.3, min_height=0.15, max_height=0.7),
    "gloves": ItemTuning(question="protective work gloves on their hands", detect_term="work glove",
                         part_name="the hands", min_width=0.01, max_width=0.5, min_height=0.01, max_height=0.25),
    "boots": ItemTuning(question="safety boots / work boots", detect_term="boot", part_name="the feet",
                        region_top=0.6, region_bottom=1.1,
                        min_width=0.02, max_width=0.7, min_height=0.01, max_height=0.35),
    "goggles": ItemTuning(question="safety goggles or safety glasses", detect_term="safety goggles",
                          part_name="the face", region_top=-0.05, region_bottom=0.35,
                          min_width=0.02, max_width=0.7, min_height=0.005, max_height=0.15),
}
# The initial method's items (settings state 1): the same wording and regions, plus the body part
# each item belongs on. Detect finds a "foot" in a dark patch under a chair or behind a wall, so for
# feet Moondream is asked instead.
INITIAL_ITEMS: dict[str, ItemTuning] = {
    "helmet": DEFAULT_ITEMS["helmet"].model_copy(update={"body_part": "head"}),
    "vest": DEFAULT_ITEMS["vest"].model_copy(),
    "gloves": DEFAULT_ITEMS["gloves"].model_copy(update={"body_part": "hand"}),
    "boots": DEFAULT_ITEMS["boots"].model_copy(update={
        "body_part": "foot",
        "body_part_question": ("Focus on the main person in the centre of this image. "
                               "Are this person's feet visible? Answer only yes or no.")}),
    "goggles": DEFAULT_ITEMS["goggles"].model_copy(update={"body_part": "face"}),
}

SETTINGS_STATES = ("1", "2")

# Items no longer offered. Settings saved before their removal (and the copies kept by old
# runs) still load: these entries are dropped instead of rejected.
RETIRED_ITEMS = ("mask",)


class Tuning(BaseModel):
    # Which method these settings are: "1", the initial prompt (a yes/no question, with the
    # double-checks on its answers), or "2", the current one (the prompt asks whether the body
    # part is visible). It only labels the settings: what each does is set by the fields below.
    # Use Tuning.for_state() for a state's defaults.
    settings_state: Literal["1", "2"] = "2"

    # --- Moondream prompts ---------------------------------------------------
    question_template: str = Field(DEFAULT_QUESTION_TEMPLATE, min_length=1, max_length=1000)
    # Settings state 1: asked about a detected box before it overrules a "no" ({term} = the item's detect term).
    verify_template: str = Field(INITIAL_VERIFY_TEMPLATE, min_length=1, max_length=500)
    # One question for all items of a worker ({items} = one "name: wording" line per item).
    one_query_per_worker: bool = False
    multi_template: str = Field(DEFAULT_MULTI_TEMPLATE, min_length=1, max_length=1500)
    items: dict[str, ItemTuning] = Field(default_factory=lambda: {k: v.model_copy() for k, v in DEFAULT_ITEMS.items()})

    # --- PPE decision rules ----------------------------------------------------
    # What an answer that is none of yes, no or not visible counts as.
    unclear_answer: Literal["present", "unverified", "missing"] = "present"
    # What a "not visible" answer counts as. Unverified: the item can't be judged, so it is neither
    # a violation nor OK. Nothing else is asked for it: the answer already says it.
    not_visible_answer: Literal["present", "unverified", "missing"] = "unverified"
    min_region_overlap: float = Field(0.6, ge=0, le=1)  # share of a detected box inside the item's region
    # Settings state 1's double-checks (off in state 2, where the prompt asks about visibility):
    confirm_no_with_detect: bool = False  # a "no" only counts if detect() can't find the item either
    verify_detected_box: bool = False  # re-ask Moondream about a detected box before it overrules a "no"
    check_body_part: bool = False  # item unverified (not missing) when its body part can't be seen
    # A "yes" for a worker who overlaps a neighbour may be about the neighbour: confirm it with detect.
    confirm_yes_on_overlap: bool = True
    overlap_threshold: float = Field(0.2, ge=0.01, le=1)  # share of the worker's box covered by others
    crop_padding: float = Field(0.05, ge=0, le=0.5)  # around each worker, as a share of the box
    min_check_height_frac: float = Field(0.08, ge=0, le=0.5)  # smaller workers are "too far"
    min_check_height_px: int = Field(100, ge=0, le=2000)  # ...and so are workers shorter than this

    # --- Status and voting ----------------------------------------------------------
    vote_window: int = Field(3, ge=1, le=9)  # video shows the majority of this many recent checks
    confirm_checks: int = Field(2, ge=1, le=9)  # an item turns red only after this many missing checks
    min_check_age_sec: float = Field(1.0, ge=0, le=10)  # a new track is checked once it's this old
    skip_edge_arrivals: bool = True  # wait to check a newcomer until they're fully in frame
    max_failed_checks: int = Field(5, ge=1, le=100)  # stop the run after this many failed checks in a row

    # --- Moondream requests -----------------------------------------------------
    # Model ID sent with every request, so results don't change when the server's default does.
    moondream_model: str = Field(DEFAULT_MODEL, min_length=1, max_length=120)
    parallel_requests: int = Field(4, ge=1, le=16)
    retry_attempts: int = Field(6, ge=1, le=8)  # per call; waits 1, 2, 4 ... s between tries

    # --- Live cameras (the camera wall) ---------------------------------------------
    # How many times each simulated camera plays its video before it stops by itself; 0 = on repeat
    # until it is stopped or removed.
    camera_loops: int = Field(0, ge=0, le=10000)

    # --- Person tracking (YOLO + BoT-SORT) -----------------------------------------
    person_model: str = Field("", max_length=120)  # file in models/; "" = server default
    detector_imgsz: int = Field(640, ge=320, le=1920)
    detect_fps: float = Field(10.0, ge=0, le=60)  # person detections per second; 0 = every frame
    detector_conf: float = Field(0.1, ge=0.01, le=0.9)
    new_track_thresh: float = Field(0.5, ge=0.05, le=0.95)
    min_track_age_sec: float = Field(0.3, ge=0, le=3)
    hidden_sec: float = Field(8.0, ge=0, le=60)
    coast_sec: float = Field(0.5, ge=0, le=3)  # keep drawing a box this long after the detector misses it
    with_reid: bool = False  # also compare appearance when matching (slower; see README)
    fixed_camera: bool = False  # skip camera-motion compensation

    @field_validator("question_template")
    @classmethod
    def _template_has_item(cls, v: str) -> str:
        if "{item}" not in v:
            raise ValueError("must contain {item}, where the item's question goes")
        return v

    @field_validator("verify_template")
    @classmethod
    def _template_has_term(cls, v: str) -> str:
        if "{term}" not in v:
            raise ValueError("must contain {term}, where the item's detect term goes")
        return v

    @field_validator("multi_template")
    @classmethod
    def _template_has_items(cls, v: str) -> str:
        if "{items}" not in v:
            raise ValueError("must contain {items}, where the list of items goes")
        return v

    @model_validator(mode="after")
    def _confirm_fits_window(self) -> "Tuning":
        if self.confirm_checks > self.vote_window:
            raise ValueError("confirm_checks can't be larger than vote_window")
        return self

    @field_validator("items", mode="before")
    @classmethod
    def _fill_items(cls, v):
        """Missing items and fields fall back to their defaults (so settings saved by an older
        version get the newer fields' defaults); retired items are dropped, unknown ones rejected."""
        v = {k: x for k, x in dict(v or {}).items() if k not in RETIRED_ITEMS}
        unknown = set(v) - set(DEFAULT_ITEMS)
        if unknown:
            raise ValueError(f"unknown PPE item(s): {', '.join(sorted(unknown))}")

        def given(x):
            return x.model_dump() if isinstance(x, ItemTuning) else dict(x or {})
        return {k: {**DEFAULT_ITEMS[k].model_dump(), **given(v.get(k))} for k in DEFAULT_ITEMS}

    @field_validator("moondream_model")
    @classmethod
    def _model_id(cls, v: str) -> str:
        v = v.strip()
        if not re.fullmatch(r"[A-Za-z0-9._\-/@:]+", v):
            raise ValueError("must be a model ID like moondream3.1-9B-A2B (no spaces)")
        return v

    @field_validator("detector_imgsz")
    @classmethod
    def _multiple_of_32(cls, v: int) -> int:
        if v % 32:
            raise ValueError("must be a multiple of 32")
        return v

    @field_validator("person_model")
    @classmethod
    def _plain_file_name(cls, v: str) -> str:
        v = v.strip()
        if v and ("/" in v or "\\" in v or v.startswith(".") or not v.endswith(MODEL_SUFFIXES)):
            raise ValueError("must be a .pt, .engine (TensorRT) or .onnx file name inside models/")
        return v

    def question_for(self, item: str) -> str:
        spec = self.items[item]
        return self.question_template.replace("{item}", spec.question).replace("{part}", spec.part_name)

    def verify_question_for(self, item: str) -> str:
        return self.verify_template.replace("{term}", self.items[item].detect_term)

    def multi_question_for(self, items: list[str]) -> str:
        # Each line names the body part when the template talks about body parts (state 2's does).
        parts = "body part" in self.multi_template.lower()
        lines = "\n".join(f"{i}: {self.items[i].question}" + (f" (body part: {self.items[i].part_name})" if parts else "")
                          for i in items)
        return self.multi_template.replace("{items}", lines)

    @classmethod
    def for_state(cls, state: str) -> "Tuning":
        """The defaults of settings state "1" (initial prompt) or "2" (current prompt)."""
        if state == "2":
            return cls()
        if state != "1":
            raise ValueError(f"unknown settings state {state!r}")
        return cls(
            settings_state="1",
            question_template=INITIAL_QUESTION_TEMPLATE,
            verify_template=INITIAL_VERIFY_TEMPLATE,
            multi_template=INITIAL_MULTI_TEMPLATE,
            items={k: v.model_copy() for k, v in INITIAL_ITEMS.items()},
            confirm_no_with_detect=True, verify_detected_box=True, check_body_part=True)

    def min_check_height(self, frame_height: int) -> float:
        """Workers shorter than this (in pixels) are too far away to check."""
        return max(self.min_check_height_frac * frame_height, self.min_check_height_px)

    def changes(self) -> list[str]:
        """Dotted names of the settings that differ from the defaults."""
        return _diff(self.model_dump(), Tuning.for_state(self.settings_state).model_dump())


# The settings that decide who is found, tracked and then checked for PPE: what the person
# detection playground lets you try without touching the saved settings.
PERSON_FIELDS = (
    "person_model", "detector_imgsz", "detect_fps", "detector_conf", "new_track_thresh",
    "min_track_age_sec", "hidden_sec", "coast_sec", "with_reid", "fixed_camera",
    "min_check_height_frac", "min_check_height_px", "min_check_age_sec",
)


def with_person_settings(overrides: dict, base: "Tuning | None" = None) -> "Tuning":
    """`base` (default: the defaults) with the person-detection settings in `overrides` applied.

    Validated like any other settings, so ranges are enforced; unknown names are an error.
    """
    unknown = set(overrides) - set(PERSON_FIELDS)
    if unknown:
        raise ValueError(f"not person detection settings: {', '.join(sorted(unknown))}")
    return Tuning.model_validate({**(base or Tuning()).model_dump(), **overrides})


def _diff(a: dict, b: dict, prefix: str = "") -> list[str]:
    out = []
    for k, v in a.items():
        name = f"{prefix}{k}"
        if isinstance(v, dict) and isinstance(b.get(k), dict):
            out += _diff(v, b[k], name + ".")
        elif v != b.get(k):
            out.append(name)
    return out
