"""API data models."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class RunStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"


class KeySource(str, Enum):
    form = "form"
    server = "server"


class RunOptions(BaseModel):
    required_ppe: list[str] = Field(default_factory=lambda: ["helmet", "vest"])
    analysis_interval_sec: float = Field(1.0, gt=0, le=30)
    max_seconds: float | None = Field(None, gt=0)


class VideoInfo(BaseModel):
    width: int
    height: int
    fps: float
    frame_count: int
    duration_sec: float


class Progress(BaseModel):
    frames_done: int = 0
    frames_total: int = 0
    frames_analysed: int = 0

    @property
    def fraction(self) -> float:
        return self.frames_done / self.frames_total if self.frames_total else 0.0


class RunMeta(BaseModel):
    """What is persisted in runs/<id>/meta.json. Never contains the API key."""

    id: str
    name: str
    created_at: datetime
    finished_at: datetime | None = None
    status: RunStatus = RunStatus.queued
    error: str | None = None
    key_source: KeySource
    options: RunOptions
    video: VideoInfo
    progress: Progress = Field(default_factory=Progress)


class TypeCount(BaseModel):
    label: str
    count: int


class TimelinePoint(BaseModel):
    time_sec: float
    workers: int
    violations: int


class ViolationEvent(BaseModel):
    time_sec: float
    frame: int
    violations: list[str]
    box: list[int]
    snapshot: str | None  # file name under /snapshots/


class RunSummary(BaseModel):
    frames_analysed: int
    peak_workers: int
    avg_workers: float
    worker_checks: int  # sum over checked frames of workers seen
    ok_checks: int
    violation_checks: int
    compliance_rate: float | None  # ok_checks / worker_checks
    frames_with_violation: int
    by_type: list[TypeCount]
    unverified: list[TypeCount]
    timeline: list[TimelinePoint]
    events: list[ViolationEvent]


class RunListItem(BaseModel):
    id: str
    name: str
    created_at: datetime
    status: RunStatus
    progress_fraction: float
    violation_checks: int | None = None
    worker_checks: int | None = None


class RunDetail(BaseModel):
    meta: RunMeta
    progress_fraction: float
    summary: RunSummary | None = None  # partial while running, final when completed


class AppConfig(BaseModel):
    server_key_configured: bool
    ppe_items: list[dict]
    defaults: RunOptions
    max_upload_mb: int


class KeyCheckResult(BaseModel):
    ok: bool
    source: KeySource | None
    message: str
