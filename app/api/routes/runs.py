"""Runs: create (upload + start), list, inspect, fetch media, delete."""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import cv2
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from ppe.config import parse_required

from ...core.settings import Settings
from ...schemas.runs import (
    KeySource, RunDetail, RunListItem, RunMeta, RunOptions, RunStatus, VideoInfo,
)
from ...services.keys import (
    KeyRejected, KeyVerifier, MissingKey, MoondreamUnreachable, resolve_key,
)
from ...services.run_store import RunNotFound, RunStore
from ...services.runner import RunRunner
from ...services.summary import build_summary
from ...services.media import VideoConverter
from ..deps import get_key_verifier, get_runner, get_settings, get_store, get_video_converter

router = APIRouter(prefix="/api/runs", tags=["runs"])

ALLOWED_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm"}
CHUNK = 1024 * 1024

Store = Annotated[RunStore, Depends(get_store)]
Runner = Annotated[RunRunner, Depends(get_runner)]


def _get_meta(store: RunStore, run_id: str) -> RunMeta:
    try:
        return store.get(run_id)
    except RunNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")


def _probe(path: Path) -> VideoInfo:
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            raise ValueError("not a readable video")
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        ok, _ = cap.read()
        if not ok:
            raise ValueError("no frames could be decoded")
        return VideoInfo(
            width=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), height=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            fps=round(fps, 3), frame_count=count, duration_sec=round(count / fps, 2),
        )
    finally:
        cap.release()


def _save_upload(upload: UploadFile, dest: Path, max_bytes: int) -> None:
    written = 0
    with dest.open("wb") as f:
        while chunk := upload.file.read(CHUNK):
            written += len(chunk)
            if written > max_bytes:
                raise ValueError(f"Video is larger than {max_bytes // CHUNK} MB")
            f.write(chunk)


def _progress_fraction(meta: RunMeta) -> float:
    if meta.status == RunStatus.completed:
        return 1.0
    return round(meta.progress.fraction, 4)


def _image_names(folder: Path) -> set[str]:
    return {p.name for p in folder.glob("*.jpg")} if folder.exists() else set()


@router.post("", response_model=RunDetail, status_code=status.HTTP_201_CREATED)
async def create_run(
    store: Store,
    runner: Runner,
    settings: Annotated[Settings, Depends(get_settings)],
    verify: Annotated[KeyVerifier, Depends(get_key_verifier)],
    video: Annotated[UploadFile, File(description="Construction-site video")],
    api_key: Annotated[str | None, Form()] = None,
    required_ppe: Annotated[str, Form(description="Comma separated, e.g. helmet,vest")] = "helmet,vest",
    analysis_interval_sec: Annotated[float, Form()] = 1.0,
    max_seconds: Annotated[float | None, Form()] = None,
) -> RunDetail:
    try:
        key, key_source = resolve_key(api_key, settings)
        options = RunOptions(required_ppe=parse_required(required_ppe),
                             analysis_interval_sec=analysis_interval_sec,
                             max_seconds=max_seconds or None)
    except (MissingKey, ValueError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))

    # Check the key before accepting the upload, so a wrong key never becomes a failed run.
    try:
        await run_in_threadpool(verify, key)
    except KeyRejected as exc:
        hint = " It came from the server's .env." if key_source == KeySource.server else ""
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"{exc}{hint} Fix the key and try again.")
    except MoondreamUnreachable as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))

    suffix = Path(video.filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                            f"Unsupported file type. Use one of: {', '.join(sorted(ALLOWED_SUFFIXES))}")

    run_id = store.new_id()
    paths = store.create_dir(run_id)
    try:
        dest = paths.root / f"input{suffix}"
        await run_in_threadpool(_save_upload, video, dest, settings.max_upload_mb * CHUNK)
        info = await run_in_threadpool(_probe, dest)
    except ValueError as exc:
        shutil.rmtree(paths.root, ignore_errors=True)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Couldn't use this video: {exc}")

    meta = RunMeta(
        id=run_id,
        name=Path(video.filename or "video").stem[:120],
        created_at=datetime.now(timezone.utc),
        key_source=key_source,
        options=options,
        video=info,
    )
    store.save(meta)
    runner.submit(run_id, key)
    return RunDetail(meta=meta, progress_fraction=0.0)


@router.get("", response_model=list[RunListItem])
def list_runs(store: Store) -> list[RunListItem]:
    items = []
    for meta in store.list():
        item = RunListItem(id=meta.id, name=meta.name, created_at=meta.created_at,
                           status=meta.status, progress_fraction=_progress_fraction(meta))
        if meta.status == RunStatus.completed and (report := store.read_report(meta.id)):
            s = build_summary(report["timeline"], available_snapshots=set(), available_frames=set(),
                              available_photos=set())
            item.violation_checks, item.worker_checks = s.violation_checks, s.worker_checks
        items.append(item)
    return items


@router.get("/{run_id}", response_model=RunDetail)
def get_run(run_id: str, store: Store, runner: Runner,
            converter: Annotated[VideoConverter, Depends(get_video_converter)]) -> RunDetail:
    meta = _get_meta(store, run_id)
    paths = store.paths(run_id)
    timeline = None
    if meta.status == RunStatus.completed:
        converter.warm(paths.output, audio_source=paths.input(), max_seconds=meta.options.max_seconds)
        report = store.read_report(run_id)
        timeline = report["timeline"] if report else None
    elif meta.status == RunStatus.running:
        timeline = runner.live_timeline(run_id)
    summary = (build_summary(timeline, _image_names(paths.snapshots), _image_names(paths.frames),
                             _image_names(paths.people))
               if timeline is not None else None)
    return RunDetail(meta=meta, progress_fraction=_progress_fraction(meta), summary=summary)


@router.delete("/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_run(run_id: str, store: Store) -> None:
    meta = _get_meta(store, run_id)
    if meta.status in (RunStatus.queued, RunStatus.running):
        raise HTTPException(status.HTTP_409_CONFLICT, "This run is still in progress")
    store.delete(run_id)


def _file(path: Path | None, media_type: str, filename: str | None = None) -> FileResponse:
    if path is None or not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not available")
    return FileResponse(path, media_type=media_type, filename=filename,
                        headers={"Cache-Control": "no-store"})


@router.get("/{run_id}/video")
def get_video(run_id: str, store: Store,
              converter: Annotated[VideoConverter, Depends(get_video_converter)],
              download: bool = False) -> FileResponse:
    meta = _get_meta(store, run_id)
    paths = store.paths(run_id)
    if meta.status == RunStatus.completed:
        # Older runs made without ffmpeg are mp4v; convert once so browsers can play them.
        converter.ensure_playable(paths.output, audio_source=paths.input(), max_seconds=meta.options.max_seconds)
    return _file(paths.output, "video/mp4", f"{meta.name}_annotated.mp4" if download else None)


@router.get("/{run_id}/report")
def get_report(run_id: str, store: Store) -> FileResponse:
    meta = _get_meta(store, run_id)
    return _file(store.paths(run_id).report, "application/json", f"{meta.name}_report.json")


@router.get("/{run_id}/preview")
def get_preview(run_id: str, store: Store) -> FileResponse:
    _get_meta(store, run_id)
    return _file(store.paths(run_id).preview, "image/jpeg")


def _image(store: RunStore, run_id: str, folder: str, name: str) -> FileResponse:
    _get_meta(store, run_id)
    if Path(name).name != name or not name.endswith(".jpg"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not available")
    return _file(getattr(store.paths(run_id), folder) / name, "image/jpeg")


@router.get("/{run_id}/snapshots/{name}")
def get_snapshot(run_id: str, name: str, store: Store) -> FileResponse:
    return _image(store, run_id, "snapshots", name)


@router.get("/{run_id}/frames/{name}")
def get_frame(run_id: str, name: str, store: Store) -> FileResponse:
    return _image(store, run_id, "frames", name)


@router.get("/{run_id}/people/{name}")
def get_person_photo(run_id: str, name: str, store: Store) -> FileResponse:
    return _image(store, run_id, "people", name)
