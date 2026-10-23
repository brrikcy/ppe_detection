"""Tunable settings for the pipeline: Moondream prompts, decision rules, request
handling and person tracking. Edited from the dashboard's Developer options;
each run keeps a copy of the settings it used.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

DEFAULT_QUESTION_TEMPLATE = (
    "Focus on the main person in the centre of this image. "
    "Is this person wearing {item}? Answer only yes or no."
)


class ItemTuning(BaseModel):
    """How one PPE item is asked about and confirmed."""

    question: str = Field(min_length=1, max_length=300)  # fills {item} in the question template
    detect_term: str = Field(min_length=1, max_length=80)  # confirms a "no" answer with detect()
    body_part: str | None = Field(None, max_length=80)  # if not found, the item is unverified
    # Where on the worker the item sits, as fractions of the person box height from the top.
    region_top: float = Field(0.0, ge=-0.5, le=1.5)
    region_bottom: float = Field(1.0, ge=-0.5, le=1.5)

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
        return self

    @property
    def region(self) -> tuple[float, float]:
        return self.region_top, self.region_bottom


DEFAULT_ITEMS: dict[str, ItemTuning] = {
    "helmet": ItemTuning(question="a hard hat / safety helmet on their head", detect_term="hard hat",
                         body_part="head", region_top=-0.1, region_bottom=0.35),
    "vest": ItemTuning(question="a high-visibility safety vest or hi-vis jacket",
                       detect_term="high visibility vest", region_top=0.1, region_bottom=0.8),
    "gloves": ItemTuning(question="protective work gloves on their hands", detect_term="work glove",
                         body_part="hand"),
    "boots": ItemTuning(question="safety boots / work boots", detect_term="boot", body_part="foot",
                        region_top=0.6, region_bottom=1.1),
    "goggles": ItemTuning(question="safety goggles or safety glasses", detect_term="safety goggles",
                          body_part="face", region_top=-0.05, region_bottom=0.35),
    "mask": ItemTuning(question="a face mask or respirator", detect_term="face mask", body_part="face",
                       region_top=-0.05, region_bottom=0.4),
}


class Tuning(BaseModel):
    # --- Moondream prompts ---------------------------------------------------
    question_template: str = Field(DEFAULT_QUESTION_TEMPLATE, min_length=1, max_length=1000)
    items: dict[str, ItemTuning] = Field(default_factory=lambda: {k: v.model_copy() for k, v in DEFAULT_ITEMS.items()})

    # --- PPE decision rules ----------------------------------------------------
    # What an answer that is neither yes nor no counts as.
    unclear_answer: Literal["present", "unverified", "missing"] = "present"
    confirm_no_with_detect: bool = True  # a "no" only counts if detect() can't find the item either
    check_body_part: bool = True  # item unverified (not missing) when its body part can't be seen
    crop_padding: float = Field(0.05, ge=0, le=0.5)  # around each worker, as a share of the box
    min_check_height_frac: float = Field(0.08, ge=0, le=0.5)  # smaller workers are "too far"
    vote_window: int = Field(3, ge=1, le=9)  # video shows the majority of this many recent checks
    person_flag_ratio: float = Field(0.5, ge=0.05, lt=1.0)  # flagged when missing in > this share

    # --- Moondream requests -----------------------------------------------------
    parallel_requests: int = Field(4, ge=1, le=16)
    retry_attempts: int = Field(6, ge=1, le=8)  # per call; waits 1, 2, 4 ... s between tries

    # --- Person tracking (YOLO + BoT-SORT) -----------------------------------------
    person_model: str = Field("", max_length=120)  # file in models/; "" = server default
    detector_imgsz: int = Field(640, ge=320, le=1920)
    detector_conf: float = Field(0.1, ge=0.01, le=0.9)
    new_track_thresh: float = Field(0.5, ge=0.05, le=0.95)
    min_track_age_sec: float = Field(0.3, ge=0, le=3)
    hidden_sec: float = Field(8.0, ge=0, le=60)

    @field_validator("question_template")
    @classmethod
    def _template_has_item(cls, v: str) -> str:
        if "{item}" not in v:
            raise ValueError("must contain {item}, where the item's question goes")
        return v

    @field_validator("items", mode="before")
    @classmethod
    def _fill_items(cls, v):
        """Missing items fall back to their defaults; unknown items are rejected."""
        v = dict(v or {})
        unknown = set(v) - set(DEFAULT_ITEMS)
        if unknown:
            raise ValueError(f"unknown PPE item(s): {', '.join(sorted(unknown))}")
        return {k: v.get(k, DEFAULT_ITEMS[k].model_dump()) for k in DEFAULT_ITEMS}

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
