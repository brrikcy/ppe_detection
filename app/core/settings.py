"""Server settings, read once from environment variables / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    server_api_key: str | None  # fallback when the form leaves the key empty
    max_upload_mb: int = 1024
    person_model: Path | None = None  # YOLO weights for person tracking; None = models/yolo11n.pt

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(PROJECT_ROOT / ".env")
        return cls(
            data_dir=Path(os.getenv("PPE_DATA_DIR", PROJECT_ROOT / "runs")).resolve(),
            server_api_key=os.getenv("MOONDREAM_API_KEY") or None,
            max_upload_mb=int(os.getenv("MAX_UPLOAD_MB", "1024")),
            person_model=(PROJECT_ROOT / os.environ["PERSON_MODEL"]).resolve()
            if os.getenv("PERSON_MODEL") else None,
        )
