"""FastAPI dependency providers. Services live on app.state, created in main.create_app.

They take an HTTPConnection, so WebSocket routes can use them too."""

from __future__ import annotations

from fastapi.requests import HTTPConnection

from ..core.settings import Settings
from ..services.dev_settings import DevSettingsStore
from ..services.keys import KeyVerifier
from ..services.media import VideoConverter
from ..services.run_store import RunStore
from ..services.runner import RunRunner


def get_settings(request: HTTPConnection) -> Settings:
    return request.app.state.settings


def get_store(request: HTTPConnection) -> RunStore:
    return request.app.state.store


def get_runner(request: HTTPConnection) -> RunRunner:
    return request.app.state.runner


def get_key_verifier(request: HTTPConnection) -> KeyVerifier:
    return request.app.state.verify_key


def get_video_converter(request: HTTPConnection) -> VideoConverter:
    return request.app.state.video_converter


def get_dev_settings(request: HTTPConnection) -> DevSettingsStore:
    return request.app.state.dev_settings
