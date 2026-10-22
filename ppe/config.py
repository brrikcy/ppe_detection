"""Runtime configuration, loaded from environment variables / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

# PPE items the pipeline knows how to check.
# key -> (label shown in a violation, question fragment sent to Moondream)
PPE_CATALOG: dict[str, tuple[str, str]] = {
    "helmet": ("No Helmet", "a hard hat / safety helmet on their head"),
    "vest": ("No Vest", "a high-visibility safety vest or hi-vis jacket"),
    "gloves": ("No Gloves", "protective work gloves on their hands"),
    "boots": ("No Boots", "safety boots / work boots"),
    "goggles": ("No Goggles", "safety goggles or safety glasses"),
    "mask": ("No Mask", "a face mask or respirator"),
}


def _parse_required(value: str | None) -> list[str]:
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
    min_person_height_px: int = 40
    crop_padding: float = 0.15

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
            required_ppe=_parse_required(os.getenv("REQUIRED_PPE")),
            analysis_interval_sec=float(os.getenv("ANALYSIS_INTERVAL_SEC", "1.0")),
            max_workers=int(os.getenv("MAX_WORKERS", "4")),
        )
        for k, v in overrides.items():
            if v is None:
                continue
            if k == "required_ppe" and isinstance(v, str):
                v = _parse_required(v)
            setattr(cfg, k, v)
        return cfg
