"""API key resolution and verification.

The key typed in the form wins; the server's .env is the fallback.
"""

from __future__ import annotations

from typing import Callable

from PIL import Image

from ppe.moondream_client import AuthError, MoondreamCloud

from ..core.settings import Settings
from ..schemas.runs import KeySource


class MissingKey(ValueError):
    pass


class KeyRejected(Exception):
    pass


class MoondreamUnreachable(Exception):
    pass


KeyVerifier = Callable[[str], None]


def resolve_key(form_key: str | None, settings: Settings) -> tuple[str, KeySource]:
    form_key = (form_key or "").strip()
    if form_key:
        return form_key, KeySource.form
    if settings.server_api_key:
        return settings.server_api_key, KeySource.server
    raise MissingKey("No API key: enter one, or set MOONDREAM_API_KEY in the server's .env.")


def verify_key(api_key: str) -> None:
    """Make one tiny Moondream call. Raises KeyRejected or MoondreamUnreachable."""
    try:
        MoondreamCloud(api_key, timeout=20).query(Image.new("RGB", (32, 32), "white"), "What color is this?")
    except AuthError as exc:
        raise KeyRejected("Moondream rejected this API key.") from exc
    except Exception as exc:
        raise MoondreamUnreachable(f"Couldn't reach Moondream to check the key: {exc}") from exc
