"""Tunable settings for the pipeline: Moondream prompts, decision rules, request
handling and person tracking. Edited from the dashboard's Developer options;
each run keeps a copy of the settings it used.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .moondream_client import DEFAULT_MODEL

DEFAULT_QUESTION_TEMPLATE = (
    "Focus on the main person in the centre of this image. "
    "Is this person wearing {item}? Answer only yes or no."
)
DEFAULT_VERIFY_TEMPLATE = "Is this a {term}? Answer only yes or no."
DEFAULT_MULTI_TEMPLATE = (
    "Focus on the main person in the centre of this image. "
    "For each line, answer yes or no:\n{items}\n"
    "Reply with one line per item, like helmet: yes"
)


class ItemTuning(BaseModel):
    """How one PPE item is asked about and confirmed."""

    question: str = Field(min_length=1, max_length=300)  # fills {item} in the question template
    detect_term: str = Field(min_length=1, max_length=80)  # confirms a "no" answer with detect()
    body_part: str | None = Field(None, max_length=80)  # if not found, the item is unverified
    # Where on the worker the item sits, as fractions of the person box height from the top.
    region_top: float = Field(0.0, ge=-0.5, le=1.5)
    region_bottom: float = Field(1.0, ge=-0.5, le=1.5)
    # Plausible size of a detected item, as a share of the worker box width / height.
    # A "vest" box the size of a helmet doesn't overrule a "no". Minimums are loose: a
    # bent-over worker has a wide box, and an item at the crop edge is detected cut off.
    min_width: float = Field(0.0, ge=0, le=2)
    max_width: float = Field(2.0, ge=0, le=2)
    min_height: float = Field(0.0, ge=0, le=2)
    max_height: float = Field(2.0, ge=0, le=2)

    @field_validator("question", "detect_term")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v

    @field_validator("body_part")
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
                         body_part="head", region_top=-0.1, region_bottom=0.35,
                         min_width=0.03, max_width=0.9, min_height=0.02, max_height=0.3),
    "vest": ItemTuning(question="a high-visibility safety vest or hi-vis jacket",
                       detect_term="high visibility vest", region_top=0.1, region_bottom=0.8,
                       min_width=0.2, max_width=1.3, min_height=0.15, max_height=0.7),
    "gloves": ItemTuning(question="protective work gloves on their hands", detect_term="work glove",
                         body_part="hand", min_width=0.01, max_width=0.5, min_height=0.01, max_height=0.25),
    "boots": ItemTuning(question="safety boots / work boots", detect_term="boot", body_part="foot",
                        region_top=0.6, region_bottom=1.1,
                        min_width=0.02, max_width=0.7, min_height=0.01, max_height=0.35),
    "goggles": ItemTuning(question="safety goggles or safety glasses", detect_term="safety goggles",
                          body_part="face", region_top=-0.05, region_bottom=0.35,
                          min_width=0.02, max_width=0.7, min_height=0.005, max_height=0.15),
    "mask": ItemTuning(question="a face mask or respirator", detect_term="face mask", body_part="face",
                       region_top=-0.05, region_bottom=0.4,
                       min_width=0.02, max_width=0.7, min_height=0.01, max_height=0.2),
}


class Tuning(BaseModel):
    # --- Moondream prompts ---------------------------------------------------
    question_template: str = Field(DEFAULT_QUESTION_TEMPLATE, min_length=1, max_length=1000)
    # Asked about a detected box before it overrules a "no" ({term} = the item's detect term).
    verify_template: str = Field(DEFAULT_VERIFY_TEMPLATE, min_length=1, max_length=500)
    # One question for all items of a worker ({items} = one "name: wording" line per item).
    one_query_per_worker: bool = False
    multi_template: str = Field(DEFAULT_MULTI_TEMPLATE, min_length=1, max_length=1500)
    items: dict[str, ItemTuning] = Field(default_factory=lambda: {k: v.model_copy() for k, v in DEFAULT_ITEMS.items()})

    # --- PPE decision rules ----------------------------------------------------
    # What an answer that is neither yes nor no counts as.
    unclear_answer: Literal["present", "unverified", "missing"] = "present"
    confirm_no_with_detect: bool = True  # a "no" only counts if detect() can't find the item either
    min_region_overlap: float = Field(0.6, ge=0, le=1)  # share of a detected box inside the item's region
    verify_detected_box: bool = True  # re-ask Moondream about a detected box before it overrules a "no"
    check_body_part: bool = True  # item unverified (not missing) when its body part can't be seen
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
        version get the newer fields' defaults); unknown items are rejected."""
        v = dict(v or {})
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
        if v and ("/" in v or "\\" in v or v.startswith(".") or not v.endswith(".pt")):
            raise ValueError("must be a .pt file name inside models/")
        return v

    def question_for(self, item: str) -> str:
        return self.question_template.replace("{item}", self.items[item].question)

    def verify_question_for(self, item: str) -> str:
        return self.verify_template.replace("{term}", self.items[item].detect_term)

    def multi_question_for(self, items: list[str]) -> str:
        lines = "\n".join(f"{i}: {self.items[i].question}" for i in items)
        return self.multi_template.replace("{items}", lines)

    def min_check_height(self, frame_height: int) -> float:
        """Workers shorter than this (in pixels) are too far away to check."""
        return max(self.min_check_height_frac * frame_height, self.min_check_height_px)

    def changes(self) -> list[str]:
        """Dotted names of the settings that differ from the defaults."""
        return _diff(self.model_dump(), Tuning().model_dump())


def _diff(a: dict, b: dict, prefix: str = "") -> list[str]:
    out = []
    for k, v in a.items():
        name = f"{prefix}{k}"
        if isinstance(v, dict) and isinstance(b.get(k), dict):
            out += _diff(v, b[k], name + ".")
        elif v != b.get(k):
            out.append(name)
    return out
