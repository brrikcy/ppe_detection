"""Server capabilities and API key checks."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form
from fastapi.concurrency import run_in_threadpool

from ppe.config import PPE_CATALOG

from ...core.settings import Settings
from ...schemas.runs import AppConfig, KeyCheckResult, RunOptions
from ...services.keys import (
    KeyRejected, KeyVerifier, MissingKey, ModelRejected, MoondreamUnreachable, endpoint_for, is_local, resolve_key,
)
from ..deps import get_key_verifier, get_settings

router = APIRouter(prefix="/api", tags=["config"])


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/config", response_model=AppConfig)
def get_config(settings: Annotated[Settings, Depends(get_settings)]) -> AppConfig:
    return AppConfig(
        server_key_configured=bool(settings.server_api_key),
        server_moondream_local=is_local(settings.moondream_endpoint),
        ppe_items=[{"key": k, "label": v.label.removeprefix("No ")} for k, v in PPE_CATALOG.items()],
        defaults=RunOptions(),
        max_upload_mb=settings.max_upload_mb,
    )


@router.post("/key/check", response_model=KeyCheckResult)
async def post_key_check(
    settings: Annotated[Settings, Depends(get_settings)],
    verify: Annotated[KeyVerifier, Depends(get_key_verifier)],
    api_key: Annotated[str | None, Form()] = None,
) -> KeyCheckResult:
    try:
        key, source = resolve_key(api_key, settings)
        endpoint = endpoint_for(source, settings)
        await run_in_threadpool(verify, key, endpoint)
    except MissingKey as exc:
        return KeyCheckResult(ok=False, source=None, message=str(exc))
    except (KeyRejected, ModelRejected, MoondreamUnreachable) as exc:
        return KeyCheckResult(ok=False, source=source, message=str(exc))
    if is_local(endpoint):
        return KeyCheckResult(ok=True, source=source, message="Moondream is running on this server; it needs no key.")
    return KeyCheckResult(ok=True, source=source, message="Key works.")
