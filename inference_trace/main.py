"""Inference flowchart: upload a video, run it through the PPE pipeline, and see every step.

    python run_trace.py            # opens http://127.0.0.1:8100

A separate FastAPI app on top of the main system's backend (see jobs.py). The page at /
uploads a video and lists traces; /#/t/<id> is the interactive flowchart of one trace.
"""

from __future__ import annotations

import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.core.settings import PROJECT_ROOT, Settings
from app.services.keys import MissingKey
from ppe.config import PPE_CATALOG

from .jobs import TraceJobs

STATIC_DIR = Path(__file__).parent / "static"
TRACES_DIR = PROJECT_ROOT / "traces"


class _NoCache(StaticFiles):
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(settings: Settings | None = None, folder: Path = TRACES_DIR,
               analyzer_factory=None, people_factory=None) -> FastAPI:
    settings = settings or Settings.from_env()
    jobs = TraceJobs(folder, settings, analyzer_factory, people_factory)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        jobs.shutdown()

    app = FastAPI(title="PPE inference flowchart", lifespan=lifespan)
    app.state.jobs = jobs

    @app.get("/api/config")
    def config() -> dict:
        tuning = jobs.dev.get()
        return {
            "items": [{"key": k, "label": v.label.removeprefix("No ")} for k, v in PPE_CATALOG.items()],
            "default_items": ["helmet", "vest"],
            "server_key_configured": bool(settings.server_api_key),
            "saved_changes": tuning.changes(),
        }

    @app.get("/api/traces")
    def list_traces() -> list[dict]:
        return jobs.list()

    @app.post("/api/traces", status_code=status.HTTP_201_CREATED)
    def create_trace(
        video: Annotated[UploadFile, File()],
        required_ppe: Annotated[str, Form()] = "helmet,vest",
        interval: Annotated[float, Form(gt=0, le=30)] = 1.0,
        max_seconds: Annotated[float | None, Form(gt=0)] = None,
        settings_choice: Annotated[str, Form(pattern="^(saved|defaults)$")] = "saved",
        api_key: Annotated[str | None, Form()] = None,
    ) -> dict:
        items = [i.strip() for i in required_ppe.split(",") if i.strip()]
        unknown = [i for i in items if i not in PPE_CATALOG]
        if not items or unknown:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                                f"Unknown PPE item(s): {', '.join(unknown)}" if unknown else "Choose at least one PPE item")

        def write(dest: Path) -> None:
            with dest.open("wb") as f:
                shutil.copyfileobj(video.file, f)

        try:
            trace_id = jobs.start(video.filename or "video.mp4", write, required_ppe=items, interval=interval,
                                  max_seconds=max_seconds, settings_choice=settings_choice, api_key=api_key)
        except MissingKey as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Couldn't use this video: {exc}")
        return {"id": trace_id}

    @app.get("/api/traces/{trace_id}")
    def get_trace(trace_id: str) -> dict:
        try:
            return jobs.get(trace_id)
        except (KeyError, FileNotFoundError):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Trace not found")

    @app.delete("/api/traces/{trace_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_trace(trace_id: str) -> None:
        try:
            jobs.delete(trace_id)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Trace not found")
        except RuntimeError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc))

    @app.get("/api/traces/{trace_id}/files/{name:path}")
    def get_file(trace_id: str, name: str) -> FileResponse:
        try:
            base = jobs.path(trace_id)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Trace not found")
        path = (base / name).resolve()
        if base not in path.parents or not path.is_file() or path.name.startswith("input"):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "File not available")
        return FileResponse(path, headers={"Cache-Control": "max-age=3600"})

    app.mount("/", _NoCache(directory=STATIC_DIR, html=True), name="static")
    return app
