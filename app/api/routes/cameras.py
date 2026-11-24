"""Live camera wall: simulated RTSP cameras playing their video in a loop."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ppe.config import parse_required

from ...core.settings import Settings
from ...services.cameras import CameraManager, StartOptions
from ...services.dev_settings import DevSettingsStore
from ...services.keys import MissingKey, endpoint_for, resolve_key
from ...services.keys import KeyVerifier
from ..deps import get_cameras, get_dev_settings, get_key_verifier, get_settings
from .runs import STREAM_WINDOW, _check_key

router = APIRouter(prefix="/api/cameras", tags=["cameras"])

Cameras = Annotated[CameraManager, Depends(get_cameras)]


class StartRequest(BaseModel):
    camera_ids: list[str] = Field(min_length=1)
    api_key: str | None = None
    required_ppe: str = "helmet,vest"
    analysis_interval_sec: float = Field(2.0, gt=0, le=30)


@router.get("")
def cameras_state(cameras: Cameras) -> dict:
    """Every camera, how the ones on the dashboard are doing, and the violations found so far."""
    return cameras.state()


@router.post("/start")
async def start_cameras(body: StartRequest, cameras: Cameras,
                        settings: Annotated[Settings, Depends(get_settings)],
                        verify: Annotated[KeyVerifier, Depends(get_key_verifier)],
                        dev: Annotated[DevSettingsStore, Depends(get_dev_settings)]) -> dict:
    """Puts cameras on the dashboard and starts their streams (cameras already on it are left alone)."""
    unknown = [c for c in body.camera_ids if not cameras.has(c)]
    if unknown:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such camera: {', '.join(unknown)}")
    try:
        key, key_source = resolve_key(body.api_key, settings)
        required = parse_required(body.required_ppe)
    except (MissingKey, ValueError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    endpoint = endpoint_for(key_source, settings)
    await _check_key(verify, key, key_source, endpoint)
    options = StartOptions(api_key=key, endpoint=endpoint, required_ppe=required,
                           analysis_interval_sec=body.analysis_interval_sec, tuning=dev.get())
    started = await run_in_threadpool(cameras.start, body.camera_ids, options)
    return {"started": started}


@router.post("/stop-all", status_code=status.HTTP_204_NO_CONTENT)
def stop_all(cameras: Cameras) -> None:
    cameras.stop_all()


@router.delete("/violations", status_code=status.HTTP_204_NO_CONTENT)
def clear_violations(cameras: Cameras) -> None:
    cameras.clear_violations()


@router.delete("/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_camera(camera_id: str, cameras: Cameras) -> None:
    """Takes a camera off the dashboard and stops its stream."""
    if not cameras.stop(camera_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That camera isn't on the dashboard")


@router.get("/{camera_id}/images/{token}/{folder}/{name}")
def camera_image(camera_id: str, token: str, folder: str, name: str, cameras: Cameras) -> FileResponse:
    path = cameras.image(camera_id, token, folder, name)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not available")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.websocket("/{camera_id}/live")
async def camera_live(ws: WebSocket, camera_id: str, cameras: Cameras, size: str = "thumb") -> None:
    """The camera's annotated stream, one JPEG per binary message (size=thumb for a tile, full to
    enlarge); closes when the camera is removed. Frames are answered like a run's live stream."""
    feed = cameras.feed(camera_id, size)
    if feed is None:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await ws.accept()
    seq = unanswered = 0
    try:
        with feed.watching():
            while True:
                while unanswered >= STREAM_WINDOW:
                    await ws.receive_text()
                    unanswered -= 1
                frame = await run_in_threadpool(feed.next_frame, seq)
                if frame is None:
                    break
                seq, jpeg = frame
                if jpeg is not None:
                    await ws.send_bytes(jpeg)
                    unanswered += 1
        await ws.close()
    except WebSocketDisconnect:
        pass
