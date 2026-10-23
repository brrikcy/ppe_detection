"""API key resolution and verification.

The key typed in the form wins; the server's .env is the fallback.
"""

from __future__ import annotations

from typing import Callable

from PIL import Image

from ppe.moondream_client import DEFAULT_ENDPOINT, DEFAULT_MODEL, AuthError, ModelError, MoondreamCloud

from ..core.settings import Settings
from ..schemas.runs import KeySource


class MissingKey(ValueError):
    pass


class KeyRejected(Exception):
    pass


class MoondreamUnreachable(Exception):
    pass


class ModelRejected(Exception):
    pass


KeyVerifier = Callable[[str], None]


def resolve_key(form_key: str | None, settings: Settings) -> tuple[str, KeySource]:
    form_key = (form_key or "").strip()
    if form_key:
        return form_key, KeySource.form
    if settings.server_api_key:
        return settings.server_api_key, KeySource.server
    raise MissingKey("No API key: enter one, or set MOONDREAM_API_KEY in the server's .env.")


def verify_key(api_key: str, endpoint: str = DEFAULT_ENDPOINT, model: str | None = DEFAULT_MODEL) -> None:
    """Make one tiny call with the endpoint and model runs will use.

    Raises KeyRejected, ModelRejected or MoondreamUnreachable.
    """
    try:
        MoondreamCloud(api_key, endpoint=endpoint, model=model, timeout=20).query(
            Image.new("RGB", (32, 32), "white"), "What color is this?")
    except AuthError as exc:
        raise KeyRejected("Moondream rejected this API key.") from exc
    except ModelError as exc:
        raise ModelRejected(f"The Moondream endpoint doesn't accept model {model!r}. "
                            "Change it in Developer options.") from exc
    except Exception as exc:
        raise MoondreamUnreachable(f"Couldn't reach Moondream at {endpoint} to check the key: {exc}") from exc
