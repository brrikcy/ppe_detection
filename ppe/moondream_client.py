"""Minimal Moondream Cloud REST client (detect + query).

Mirrors the official `moondream` SDK's cloud calls without pulling in its
local-inference dependencies.
"""

from __future__ import annotations

import base64
import io

import requests
from PIL import Image

DEFAULT_ENDPOINT = "https://api.moondream.ai/v1"
DEFAULT_MODEL = "moondream3.1-9B-A2B"


class AuthError(RuntimeError):
    """The API key was missing or rejected (HTTP 401/403)."""


class ModelError(RuntimeError):
    """The endpoint doesn't accept the requested model (HTTP 400 mentioning the model)."""


class MoondreamCloud:
    """Client for the Moondream REST API: Moondream Cloud, or any server with the same API
    (another provider or a self-hosted one) at `endpoint`."""

    def __init__(self, api_key: str, endpoint: str = DEFAULT_ENDPOINT, model: str | None = DEFAULT_MODEL,
                 timeout: float = 60):
        self.endpoint = endpoint.rstrip("/")
        self.model = model  # sent with every call; None lets the server pick its default
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "X-Moondream-Auth": api_key,
        })

    @staticmethod
    def _encode(image: Image.Image) -> str:
        buf = io.BytesIO()
        image.convert("RGB").save(buf, format="JPEG", quality=95)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

    def _post(self, path: str, payload: dict) -> dict:
        if self.model:
            payload = {**payload, "model": self.model}
        r = self.session.post(f"{self.endpoint}/{path}", json=payload, timeout=self.timeout)
        if r.status_code in (401, 403):
            raise AuthError(f"Moondream rejected the API key (HTTP {r.status_code})")
        if r.status_code == 400 and "model" in r.text.lower():
            raise ModelError(f"{self.endpoint} doesn't accept model {self.model!r}: {r.text[:300]}")
        if r.status_code >= 400:
            raise RuntimeError(f"Moondream /{path} HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    def detect(self, image: Image.Image, obj: str) -> dict:
        """Returns {"objects": [{"x_min", "y_min", "x_max", "y_max"}, ...]} (normalised)."""
        return self._post("detect", {"image_url": self._encode(image), "object": obj})

    def query(self, image: Image.Image, question: str) -> dict:
        """Returns {"answer": str}."""
        return self._post("query", {"image_url": self._encode(image), "question": question,
                                    "stream": False})
