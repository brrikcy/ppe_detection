"""Runtime configuration, loaded from environment variables / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

from .moondream_client import DEFAULT_ENDPOINT
from .tuning import Tuning


@dataclass(frozen=True)
class PPEItem:
    label: str  # shown in a violation, e.g. "No Helmet"


# PPE items the pipeline knows how to check. How each one is asked about and confirmed
# (question, detect term, body part, region) is tunable: see ppe.tuning.
PPE_CATALOG: dict[str, PPEItem] = {
    "helmet": PPEItem("No Helmet"),
    "vest": PPEItem("No Vest"),
    "gloves": PPEItem("No Gloves"),
    "boots": PPEItem("No Boots"),
    "goggles": PPEItem("No Goggles"),
    "mask": PPEItem("No Mask"),
}


def endpoint_from_env() -> str:
    """MOONDREAM_ENDPOINT, or Moondream Cloud. Must be an http(s) base URL like https://host/v1."""
    url = (os.getenv("MOONDREAM_ENDPOINT") or DEFAULT_ENDPOINT).strip().rstrip("/")
    if not url.startswith(("https://", "http://")):
        raise ValueError(f"MOONDREAM_ENDPOINT must start with https:// or http://, got {url!r}")
    return url


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
    person_model: str | None = None  # server default YOLO weights; None = models/yolo11s.pt
    tuning: Tuning = field(default_factory=Tuning)  # prompts, decision rules, model, tracker settings
    moondream_endpoint: str = DEFAULT_ENDPOINT  # Moondream API base URL (MOONDREAM_ENDPOINT)

    @classmethod
    def from_env(cls, tuning: Tuning | None = None, **overrides) -> "Config":
        load_dotenv()
        api_key = overrides.pop("api_key", None) or os.getenv("MOONDREAM_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "MOONDREAM_API_KEY is not set. Copy .env.example to .env and add your key."
            )
        if tuning is None:
            tuning = Tuning()
            if os.getenv("MAX_WORKERS"):
                tuning = tuning.model_copy(update={"parallel_requests": int(os.environ["MAX_WORKERS"])})
        cfg = cls(
            api_key=api_key,
            required_ppe=parse_required(os.getenv("REQUIRED_PPE")),
            analysis_interval_sec=float(os.getenv("ANALYSIS_INTERVAL_SEC", "1.0")),
            person_model=os.getenv("PERSON_MODEL") or None,
            tuning=tuning,
            moondream_endpoint=endpoint_from_env(),
        )
        for k, v in overrides.items():
            if v is None:
                continue
            if k == "required_ppe" and isinstance(v, str):
                v = parse_required(v)
            setattr(cfg, k, v)
        return cfg
