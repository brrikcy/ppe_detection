"""FastAPI application factory.

    uvicorn app.main:create_app --factory      # or: python run.py
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .api.routes import config as config_routes
from .api.routes import runs as runs_routes
from .core.settings import Settings
from .services.keys import KeyVerifier, verify_key
from .services.media import VideoConverter
from .services.run_store import RunStore
from .services.runner import AnalyzerFactory, RunRunner, default_analyzer_factory

STATIC_DIR = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None,
               analyzer_factory: AnalyzerFactory = default_analyzer_factory,
               key_verifier: KeyVerifier = verify_key) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.store.mark_interrupted()
        yield
        app.state.runner.shutdown()

    app = FastAPI(title="PPE Inspector", version="0.1.0", lifespan=lifespan)
    store = RunStore(settings.data_dir)
    app.state.settings = settings
    app.state.store = store
    app.state.verify_key = key_verifier
    app.state.video_converter = VideoConverter()
    app.state.runner = RunRunner(store, analyzer_factory, max_workers=settings.max_workers)

    app.include_router(config_routes.router)
    app.include_router(runs_routes.router)
    # Registered last so /api routes take precedence.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
