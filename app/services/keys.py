"""API key resolution: the key typed in the form wins, the server's .env is the fallback."""

from __future__ import annotations

from PIL import Image

from ppe.moondream_client import AuthError, MoondreamCloud

from ..core.settings import Settings
from ..schemas.runs import KeySource


class MissingKey(ValueError):
    pass


def resolve_key(form_key: str | None, settings: Settings) -> tuple[str, KeySource]:
    form_key = (form_key or "").strip()
    if form_key:
        return form_key, KeySource.form
    if settings.server_api_key:
        return settings.server_api_key, KeySource.server
    raise MissingKey("No API key: enter one, or set MOONDREAM_API_KEY in the server's .env.")


def check_key(api_key: str) -> tuple[bool, str]:
    """Make one tiny Moondream call to verify the key."""
    try:
        MoondreamCloud(api_key, timeout=20).query(Image.new("RGB", (32, 32), "white"), "What color is this?")
    except AuthError:
        return False, "Moondream rejected this API key."
    except Exception as exc:
        return False, f"Couldn't reach Moondream: {exc}"
    return True, "Key works."
