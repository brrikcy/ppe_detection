"""Server capabilities and API key checks."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form
from fastapi.concurrency import run_in_threadpool

from ppe.config import PPE_CATALOG

from ...core.settings import Settings
from ...schemas.runs import AppConfig, KeyCheckResult, RunOptions
from ...services.keys import MissingKey, check_key, resolve_key
from ..deps import get_settings

router = APIRouter(prefix="/api", tags=["config"])


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/config", response_model=AppConfig)
def get_config(settings: Annotated[Settings, Depends(get_settings)]) -> AppConfig:
    return AppConfig(
        server_key_configured=bool(settings.server_api_key),
        ppe_items=[{"key": k, "label": v.label.removeprefix("No ")} for k, v in PPE_CATALOG.items()],
        defaults=RunOptions(),
        max_upload_mb=settings.max_upload_mb,
    )


@router.post("/key/check", response_model=KeyCheckResult)
async def post_key_check(
    settings: Annotated[Settings, Depends(get_settings)],
    api_key: Annotated[str | None, Form()] = None,
) -> KeyCheckResult:
    try:
        key, source = resolve_key(api_key, settings)
    except MissingKey as exc:
        return KeyCheckResult(ok=False, source=None, message=str(exc))
    ok, message = await run_in_threadpool(check_key, key)
    return KeyCheckResult(ok=ok, source=source, message=message)
