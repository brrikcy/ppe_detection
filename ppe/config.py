"""Runtime configuration, loaded from environment variables / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


@dataclass(frozen=True)
class PPEItem:
    label: str  # shown in a violation, e.g. "No Helmet"
    question: str  # fragment for "Is this person wearing ...?"
    detect_term: str  # object name used to confirm a "no" answer with detect()
    body_part: str | None = None  # if this can't be found, the item is unverifiable, not a violation
    # Where on the worker the item (and its body part) sits, as fractions of the person box
    # height from the top. A detection elsewhere, or on a neighbour, doesn't count.
    region: tuple[float, float] = (0.0, 1.0)


# PPE items the pipeline knows how to check.
PPE_CATALOG: dict[str, PPEItem] = {
    "helmet": PPEItem("No Helmet", "a hard hat / safety helmet on their head", "hard hat", "head", (-0.1, 0.35)),
    "vest": PPEItem("No Vest", "a high-visibility safety vest or hi-vis jacket", "high visibility vest",
                    region=(0.1, 0.8)),
    "gloves": PPEItem("No Gloves", "protective work gloves on their hands", "work glove", "hand"),
    "boots": PPEItem("No Boots", "safety boots / work boots", "boot", "foot", (0.6, 1.1)),
    "goggles": PPEItem("No Goggles", "safety goggles or safety glasses", "safety goggles", "face", (-0.05, 0.35)),
    "mask": PPEItem("No Mask", "a face mask or respirator", "face mask", "face", (-0.05, 0.4)),
}


def parse_required(value: str | None) -> list[str]:
    items = [v.strip().lower() for v in (value or "helmet,vest").split(",") if v.strip()]
    unknown = [i for i in items if i not in PPE_CATALOG]
    if unknown:
        raise ValueError(
            f"Unknown PPE item(s) {unknown}. Choose from: {', '.join(PPE_CATALOG)}"
        )
    return items


@dataclass
class Config:
    api_key: str
    required_ppe: list[str] = field(default_factory=lambda: ["helmet", "vest"])
    analysis_interval_sec: float = 1.0
    max_workers: int = 4
    min_check_height_frac: float = 0.08  # smaller workers (vs frame height) are tracked but not checked
    person_model: str | None = None  # YOLO weights for person tracking; None = models/yolo11n.pt
    person_imgsz: int = 640  # detector input size
    crop_padding: float = 0.05  # wider crops pull in neighbours, which sways the yes/no answer

    @classmethod
    def from_env(cls, **overrides) -> "Config":
        load_dotenv()
        api_key = overrides.pop("api_key", None) or os.getenv("MOONDREAM_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "MOONDREAM_API_KEY is not set. Copy .env.example to .env and add your key."
            )
        cfg = cls(
            api_key=api_key,
            required_ppe=parse_required(os.getenv("REQUIRED_PPE")),
            analysis_interval_sec=float(os.getenv("ANALYSIS_INTERVAL_SEC", "1.0")),
            max_workers=int(os.getenv("MAX_WORKERS", "4")),
            person_model=os.getenv("PERSON_MODEL") or None,
        )
        for k, v in overrides.items():
            if v is None:
                continue
            if k == "required_ppe" and isinstance(v, str):
                v = parse_required(v)
            setattr(cfg, k, v)
        return cfg
