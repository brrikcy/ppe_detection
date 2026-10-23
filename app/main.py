"""FastAPI application factory.

    uvicorn app.main:create_app --factory      # or: python run.py
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .api.routes import config as config_routes
from .api.routes import dev as dev_routes
from .api.routes import runs as runs_routes
from .core.settings import Settings
from .services.dev_settings import DevSettingsStore
from .services.keys import KeyVerifier, verify_key
from .services.media import VideoConverter
from .services.run_store import RunStore
from .services.runner import AnalyzerFactory, PeopleFactory, RunRunner, default_analyzer_factory

STATIC_DIR = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None,
               analyzer_factory: AnalyzerFactory = default_analyzer_factory,
               key_verifier: KeyVerifier = verify_key,
               people_factory: PeopleFactory | None = None) -> FastAPI:
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
    app.state.dev_settings = DevSettingsStore(settings.data_dir)
    app.state.runner = RunRunner(store, analyzer_factory, people_factory=people_factory,
                                 person_model=settings.person_model)

    app.include_router(config_routes.router)
    app.include_router(runs_routes.router)
    app.include_router(dev_routes.router)
    # Registered last so /api routes take precedence.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
