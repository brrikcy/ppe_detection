"""API data models."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from ppe.tuning import Tuning


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
    # Developer settings in force when the run was created (defaults for older runs).
    tuning: Tuning = Field(default_factory=Tuning)


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
    moondream_endpoint: str | None = None  # where this run's Moondream requests went (None: older runs)
    options: RunOptions
    video: VideoInfo
    progress: Progress = Field(default_factory=Progress)


class TypeCount(BaseModel):
    label: str
    count: int


class TimelinePoint(BaseModel):
    """One regular check. workers = on screen; ok = workers - violations - unverified - too_far - unchecked."""

    time_sec: float
    workers: int
    violations: int  # status shows a missing item (the same vote as the video)
    too_far: int = 0  # on screen but too small to check
    unverified: int = 0  # an item couldn't be seen
    unchecked: int = 0  # too new to check, not enough checks for a verdict yet, or the check failed


class ViolationEvent(BaseModel):
    """A violation incident: consecutive checks of one person whose status showed a missing item."""

    time_sec: float  # start
    end_sec: float | None = None  # last check in the incident (None: a single check, older runs)
    checks: int = 1
    frame: int  # the check whose photo is shown
    violations: list[str]
    box: list[int]
    track_id: int | None = None  # person id; None for runs made before tracking existed
    snapshot: str | None  # crop of the worker, file name under /snapshots/
    frame_image: str | None = None  # whole annotated frame, file name under /frames/


class PersonSummary(BaseModel):
    """One tracked worker across the whole video."""

    id: int
    first_seen_sec: float
    last_seen_sec: float
    checks: int  # checks with an answer from Moondream (failed ones not included)
    violation_checks: int  # of those, how many had any missing item (single checks, before voting)
    unverified_checks: int = 0  # of those, how many had an item that couldn't be seen
    failed_checks: int = 0
    incidents: int = 0  # violation incidents (see ViolationEvent)
    violation_sec: float = 0.0  # time from the first to the last check of each incident, summed
    state: str = "ok"  # flagged, unverified (not verified), brief (seen briefly) or ok
    ok: bool  # state == "ok"
    status_label: str  # "PPE OK", "PPE Violation (n) - No X, No Y", "Not verified" or "Seen briefly"
    missing: list[TypeCount]  # per violation label: single checks it was missing in
    latest_label: str  # status after their last check
    photo: str | None  # file name under /people/


class RunSummary(BaseModel):
    frames_analysed: int  # regular checks (arrival checks not included)
    arrival_checks: int = 0  # extra checks of workers who appeared between regular checks
    peak_workers: int
    avg_workers: float
    worker_checks: int  # worker checks Moondream answered (too far, new and failed ones excluded)
    ok_checks: int  # status PPE OK after the check
    violation_checks: int  # status showed a missing item after the check
    unverified_checks: int = 0  # status OK but an item couldn't be seen
    pending_checks: int = 0  # not enough checks of that worker yet for a verdict
    failed_checks: int = 0  # every Moondream call of the check failed
    compliance_rate: float | None  # ok_checks / (ok_checks + violation_checks)
    frames_with_violation: int
    by_type: list[TypeCount]
    unverified: list[TypeCount]
    too_small_checks: int = 0  # worker sightings too far away to check
    timeline: list[TimelinePoint]
    events: list[ViolationEvent]  # violation incidents
    tracked: bool = False  # run has person ids
    people: list[PersonSummary] = Field(default_factory=list)


class RunListItem(BaseModel):
    id: str
    name: str
    created_at: datetime
    status: RunStatus
    progress_fraction: float
    violation_checks: int | None = None
    worker_checks: int | None = None
    incidents: int | None = None


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


class TuningState(BaseModel):
    active: Tuning  # applied to new runs
    defaults: Tuning
    changes: list[str]  # dotted names of settings that differ from the defaults
    models: list[str]  # person-detection weights available in models/
    default_model: str
    moondream_endpoint: str  # server setting (MOONDREAM_ENDPOINT), shown read-only


class TryRequestResult(BaseModel):
    item: str
    status: str  # present / missing / unverified
    steps: list[dict]
    image_size: list[int]
    image: str  # the image Moondream was shown (data: URI); detect boxes are relative to it
    mode: str  # "run" (worker as in the run: crop, padding, neighbours) or "image" (the image alone)
    note: str
