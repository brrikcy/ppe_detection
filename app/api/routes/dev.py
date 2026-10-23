"""Developer options: tune prompts, decision rules and tracking; try a prompt on one image."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError

from ppe.config import PPE_CATALOG, Config
from ppe.moondream_client import AuthError
from ppe.people import DEFAULT_MODEL, MODELS_DIR
from ppe.tuning import Tuning

from ...core.settings import Settings
from ...schemas.runs import TryRequestResult, TuningState
from ...services.dev_settings import DevSettingsStore
from ...services.keys import MissingKey, resolve_key
from ...services.run_store import RunNotFound, RunStore
from ...services.runner import RunRunner
from ..deps import get_dev_settings, get_runner, get_settings, get_store

router = APIRouter(prefix="/api/dev", tags=["developer"])

DevStore = Annotated[DevSettingsStore, Depends(get_dev_settings)]
IMAGE_FOLDERS = {"people", "snapshots", "frames"}
MAX_TRY_IMAGE_BYTES = 20 * 1024 * 1024


def _state(active: Tuning, settings: Settings) -> TuningState:
    return TuningState(
        active=active,
        defaults=Tuning(),
        changes=active.changes(),
        models=sorted(p.name for p in MODELS_DIR.glob("*.pt")) if MODELS_DIR.exists() else [],
        default_model=Path(settings.person_model or DEFAULT_MODEL).name,
    )


@router.get("/tuning", response_model=TuningState)
def get_tuning(dev: DevStore, settings: Annotated[Settings, Depends(get_settings)]) -> TuningState:
    return _state(dev.get(), settings)


@router.put("/tuning", response_model=TuningState)
def put_tuning(tuning: Tuning, dev: DevStore,
               settings: Annotated[Settings, Depends(get_settings)]) -> TuningState:
    if tuning.person_model and not (MODELS_DIR / tuning.person_model).exists():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            f"Model file models/{tuning.person_model} doesn't exist")
    return _state(dev.save(tuning), settings)


@router.delete("/tuning", response_model=TuningState)
def reset_tuning(dev: DevStore, settings: Annotated[Settings, Depends(get_settings)]) -> TuningState:
    return _state(dev.reset(), settings)


def _load_image(data: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return img.convert("RGB")
    except (UnidentifiedImageError, OSError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Couldn't read that image")


@router.post("/try", response_model=TryRequestResult)
async def try_prompt(
    dev: DevStore,
    store: Annotated[RunStore, Depends(get_store)],
    runner: Annotated[RunRunner, Depends(get_runner)],
    settings: Annotated[Settings, Depends(get_settings)],
    item: Annotated[str, Form()],
    tuning: Annotated[str | None, Form(description="Tuning as JSON; default: the saved settings")] = None,
    api_key: Annotated[str | None, Form()] = None,
    image: Annotated[UploadFile | None, File()] = None,
    run_id: Annotated[str | None, Form()] = None,
    folder: Annotated[str | None, Form()] = None,
    name: Annotated[str | None, Form()] = None,
) -> TryRequestResult:
    """Check one PPE item on one worker image and return every step, without saving anything.

    The image is either uploaded, or a worker image from a run (run_id + folder + name).
    """
    if item not in PPE_CATALOG:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown PPE item: {item}")
    try:
        tun = Tuning.model_validate(json.loads(tuning)) if tuning else dev.get()
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Invalid settings: {exc}")
    try:
        key, _ = resolve_key(api_key, settings)
    except MissingKey as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))

    if image is not None:
        data = await image.read(MAX_TRY_IMAGE_BYTES + 1)
        if len(data) > MAX_TRY_IMAGE_BYTES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Image is larger than 20 MB")
    elif run_id and folder in IMAGE_FOLDERS and name and Path(name).name == name:
        try:
            path = getattr(store.paths(run_id), folder) / name
        except RunNotFound:
            path = None
        if path is None or not path.exists():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Image not found")
        data = path.read_bytes()
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Upload an image or pick one from a run")
    img = _load_image(data)

    analyzer = runner.analyzer_factory(Config(api_key=key, required_ppe=[item], tuning=tun))
    try:
        result = await run_in_threadpool(analyzer.explain_item, img, item)
    except AuthError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Moondream rejected the API key.")
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Moondream call failed: {exc}")
    finally:
        analyzer.close()
    return TryRequestResult(**result, image_size=list(img.size))
