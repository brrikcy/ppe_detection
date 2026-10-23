"""Developer options: tune prompts, decision rules and tracking; try a prompt on one image."""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Annotated

import cv2
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError

from ppe.config import PPE_CATALOG, Config
from ppe.moondream_client import AuthError, ModelError
from ppe.people import DEFAULT_MODEL, MODELS_DIR
from ppe.tuning import MODEL_SUFFIXES, Tuning

from ...core.settings import Settings
from ...schemas.runs import TryRequestResult, TuningState
from ...services.dev_settings import DevSettingsStore
from ...services.keys import MissingKey, resolve_key
from ...services.run_store import RunNotFound, RunStore
from ...services.runner import RunRunner
from ...services.summary import snapshot_name
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
        models=sorted(p.name for p in MODELS_DIR.iterdir() if p.suffix in MODEL_SUFFIXES) if MODELS_DIR.exists() else [],
        default_model=Path(settings.person_model or DEFAULT_MODEL).name,
        moondream_endpoint=settings.moondream_endpoint,
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


def _worker_in_run(store: RunStore, runner: RunRunner, run_id: str, name: str):
    """For a violation snapshot: the original frame, the worker's box and the other workers' boxes,
    so the playground checks the worker exactly as the run did. None if it can't be found."""
    report = store.read_report(run_id)
    timeline = report["timeline"] if report else runner.live_timeline(run_id) or []
    for entry in timeline:
        for i, w in enumerate(entry["workers"]):
            if snapshot_name(entry["frame"], i) != name:
                continue
            cap = cv2.VideoCapture(str(store.paths(run_id).input()))
            try:
                cap.set(cv2.CAP_PROP_POS_FRAMES, entry["frame"])
                ok, frame = cap.read()
            finally:
                cap.release()
            if not ok:
                return None
            others = [o["box"] for o in entry["workers"] if o is not w and not o.get("too_small")]
            image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            return image, w["box"], others, entry
    return None


def _data_uri(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _load_image(data: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return img.convert("RGB")
    except (UnidentifiedImageError, OSError, ImportError):
        # ImportError: once Ultralytics is loaded, it hooks HEIF support into Pillow and an
        # unreadable file can fail on that optional plugin instead.
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

    The image is either uploaded, or a worker image from a run (run_id + folder + name). A
    violation snapshot is checked as in the run: on the original frame, with the same crop,
    padding and neighbouring workers. Other images are checked on their own.
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

    found = None
    if image is None and folder == "snapshots":
        found = await run_in_threadpool(_worker_in_run, store, runner, run_id, name)
    if found:
        img, box, others, entry = found
        note = (f"Checked as in the run: frame {entry['frame']} ({entry['time_sec']} s), the same crop and "
                f"padding, with {len(others)} other worker{'' if len(others) == 1 else 's'} in the frame.")
    else:
        img, box, others = _load_image(data), None, ()
        note = "Checked on this image alone: the whole image is the worker, with no neighbours."

    analyzer = runner.analyzer_factory(Config(api_key=key, required_ppe=[item], tuning=tun,
                                              moondream_endpoint=settings.moondream_endpoint))
    try:
        result = await run_in_threadpool(analyzer.explain_item, img, item, box, others)
    except AuthError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Moondream rejected the API key.")
    except ModelError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            f"The Moondream endpoint doesn't accept model {tun.moondream_model!r}.")
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Moondream call failed: {exc}")
    finally:
        analyzer.close()
    crop = result.pop("crop")
    return TryRequestResult(**result, image_size=list(crop.size), image=_data_uri(crop),
                            mode="run" if found else "image", note=note)
