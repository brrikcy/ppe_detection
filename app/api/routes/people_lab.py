"""Person detection playground: try person-detection settings on a video without saving them."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import ValidationError

from ppe.people import MODELS_DIR
from ppe.tuning import with_person_settings

from ...core.settings import Settings
from ...schemas.people import PeopleTestInfo
from ...services.people_lab import PeoplePlayground
from ...services.run_store import RunNotFound, RunStore
from ..deps import get_people_lab, get_settings, get_store
from .runs import ALLOWED_SUFFIXES, CHUNK, _probe, _save_upload

router = APIRouter(prefix="/api/dev/people", tags=["developer"])

Lab = Annotated[PeoplePlayground, Depends(get_people_lab)]


def _get(lab: PeoplePlayground, test_id: str) -> PeopleTestInfo:
    info = lab.get(test_id)
    if info is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Test not found")
    return info


@router.get("", response_model=list[PeopleTestInfo])
def list_tests(lab: Lab) -> list[PeopleTestInfo]:
    """Tests since the server started, newest first."""
    return lab.list()


@router.post("", response_model=PeopleTestInfo, status_code=status.HTTP_201_CREATED)
async def start_test(
    lab: Lab,
    store: Annotated[RunStore, Depends(get_store)],
    settings: Annotated[Settings, Depends(get_settings)],
    person_settings: Annotated[str, Form(description="Person detection settings as JSON; omitted ones use the defaults")] = "{}",
    video: Annotated[UploadFile | None, File(description="A video to test")] = None,
    run_id: Annotated[str | None, Form(description="Or the video of an existing run")] = None,
    max_seconds: Annotated[float | None, Form(gt=0)] = None,
) -> PeopleTestInfo:
    """Track people through a video with these settings. Nothing is saved: the saved settings
    and other runs are not affected."""
    try:
        tuning = with_person_settings(json.loads(person_settings))
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Invalid settings: {exc}")
    if tuning.person_model and not (MODELS_DIR / tuning.person_model).exists():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            f"Model file models/{tuning.person_model} doesn't exist")

    if video is not None:
        suffix = Path(video.filename or "").suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                                f"Unsupported file type. Use one of: {', '.join(sorted(ALLOWED_SUFFIXES))}")
        test_id, folder = lab.new_folder()
        input_path = folder / f"input{suffix}"
        source = Path(video.filename or "video").name[:120]
        try:
            await run_in_threadpool(_save_upload, video, input_path, settings.max_upload_mb * CHUNK)
            await run_in_threadpool(_probe, input_path)
        except ValueError as exc:
            shutil.rmtree(folder, ignore_errors=True)
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Couldn't use this video: {exc}")
    elif run_id:
        try:
            meta = store.get(run_id)
        except RunNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
        input_path = store.paths(run_id).input()
        if input_path is None or not input_path.exists():
            raise HTTPException(status.HTTP_409_CONFLICT, "This run's video is no longer on disk")
        test_id, _ = lab.new_folder()
        source = meta.name
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Upload a video or pick one from a run")
    return lab.start(test_id, source, input_path, tuning, max_seconds)


@router.get("/{test_id}", response_model=PeopleTestInfo)
def get_test(test_id: str, lab: Lab) -> PeopleTestInfo:
    return _get(lab, test_id)


@router.get("/{test_id}/video")
def get_test_video(test_id: str, lab: Lab) -> FileResponse:
    info = _get(lab, test_id)
    path = lab.video_path(test_id)
    if info.status != "completed" or not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Video not available")
    return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "no-store"})


@router.delete("/{test_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_test(test_id: str, lab: Lab) -> None:
    """Deletes a finished test, or stops a running one."""
    if not lab.delete(test_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Test not found")
