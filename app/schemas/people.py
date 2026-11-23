"""Person detection playground: one test of person-detection settings on a video."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .runs import RunStatus


class PeopleTestInfo(BaseModel):
    id: str
    source: str  # video file name or run name
    status: RunStatus
    created_at: str
    max_seconds: float | None = None
    settings: dict[str, Any]  # the person-detection settings this test used
    changes: list[str] = Field(default_factory=list)  # names of those that differ from the defaults
    frames_done: int = 0
    frames_total: int = 0
    progress_fraction: float = 0.0
    error: str | None = None
    report: dict[str, Any] | None = None  # ppe.people_playground.detect_people's report once completed
