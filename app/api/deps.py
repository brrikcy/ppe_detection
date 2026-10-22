"""FastAPI dependency providers. Services live on app.state, created in main.create_app."""

from __future__ import annotations

from fastapi import Request

from ..core.settings import Settings
from ..services.keys import KeyVerifier
from ..services.media import VideoConverter
from ..services.run_store import RunStore
from ..services.runner import RunRunner


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_store(request: Request) -> RunStore:
    return request.app.state.store


def get_runner(request: Request) -> RunRunner:
    return request.app.state.runner


def get_key_verifier(request: Request) -> KeyVerifier:
    return request.app.state.verify_key


def get_video_converter(request: Request) -> VideoConverter:
    return request.app.state.video_converter
