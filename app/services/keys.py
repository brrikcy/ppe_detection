"""API key resolution and verification.

The key typed in the form wins; the server's .env is the fallback. A key typed in the form is a
Moondream Cloud key, so it goes to Moondream Cloud (MOONDREAM_CLOUD_ENDPOINT). The server's key
goes to the server's MOONDREAM_ENDPOINT, which on the GPU server is Moondream on the same machine.
"""

from __future__ import annotations

from typing import Callable
from urllib.parse import urlsplit

import requests
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


KeyVerifier = Callable[[str, str], None]  # (key, endpoint)


def resolve_key(form_key: str | None, settings: Settings) -> tuple[str, KeySource]:
    form_key = (form_key or "").strip()
    if form_key:
        return form_key, KeySource.form
    if settings.server_api_key:
        return settings.server_api_key, KeySource.server
    raise MissingKey("No API key: enter one, or set MOONDREAM_API_KEY in the server's .env.")


def endpoint_for(source: KeySource, settings: Settings) -> str:
    return settings.cloud_endpoint if source == KeySource.form else settings.moondream_endpoint


def is_local(endpoint: str) -> bool:
    """Moondream on this machine (moondream_server.serve), which doesn't check keys."""
    return urlsplit(endpoint).hostname in ("127.0.0.1", "localhost", "::1")


def verify_key(api_key: str, endpoint: str = DEFAULT_ENDPOINT, model: str | None = DEFAULT_MODEL) -> None:
    """Make one tiny call with the endpoint and model runs will use.

    Moondream on this machine has no key to check, so it only has to be running: a query there
    would wait for the GPU, behind any run that is still in progress.

    Raises KeyRejected, ModelRejected or MoondreamUnreachable.
    """
    if is_local(endpoint):
        try:
            requests.get(endpoint.removesuffix("/v1") + "/health", timeout=5).raise_for_status()
        except requests.RequestException as exc:
            raise MoondreamUnreachable(f"Moondream isn't running at {endpoint}: {exc}") from exc
        return
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
