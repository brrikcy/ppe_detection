"""Person detection playground: tests of person-detection settings on videos.

Each test tracks people through one video with its own settings (nothing saved, nothing
shared with runs) and keeps the annotated video and a report until it is deleted or the
server restarts. Tests run one at a time, since the detector already uses all the CPU/GPU.
Files are kept under <data_dir>/_people_playground/<test id>/.
"""

from __future__ import annotations

import logging
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ppe.people_playground import Cancelled, detect_people
from ppe.tuning import Tuning

from ..schemas.people import PeopleTestInfo
from ..schemas.runs import RunStatus

log = logging.getLogger(__name__)

MAX_KEPT = 12  # finished tests kept; the oldest go first


@dataclass
class _Test:
    id: str
    source: str
    tuning: Tuning
    max_seconds: float | None
    created_at: str
    status: RunStatus = RunStatus.queued
    frames_done: int = 0
    frames_total: int = 0
    error: str | None = None
    report: dict | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    discard: bool = False  # deleted while running: clean up when it stops


class PeoplePlayground:
    def __init__(self, data_dir: Path, people_factory: Callable[[float], object] | None = None,
                 default_model: Path | None = None):
        self.root = data_dir / "_people_playground"
        shutil.rmtree(self.root, ignore_errors=True)  # tests don't survive a restart
        self._people_factory = people_factory
        self._default_model = default_model
        self._tests: dict[str, _Test] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="people-test")

    # -- files ------------------------------------------------------------------
    def folder(self, test_id: str) -> Path:
        return self.root / test_id

    def video_path(self, test_id: str) -> Path:
        return self.folder(test_id) / "people.mp4"

    def new_folder(self) -> tuple[str, Path]:
        """A new test id and its (created) folder, for the route to put an uploaded video in."""
        test_id = uuid.uuid4().hex[:12]
        folder = self.folder(test_id)
        folder.mkdir(parents=True, exist_ok=True)
        return test_id, folder

    # -- tests ------------------------------------------------------------------
    def start(self, test_id: str, source: str, input_path: Path, tuning: Tuning,
              max_seconds: float | None) -> PeopleTestInfo:
        test = _Test(id=test_id, source=source, tuning=tuning, max_seconds=max_seconds,
                     created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        with self._lock:
            self._tests[test_id] = test
        self._pool.submit(self._run, test, input_path)
        self._prune()
        return self.info(test)

    def _run(self, test: _Test, input_path: Path) -> None:
        if test.cancel.is_set():
            return self._finish(test)
        test.status = RunStatus.running

        def progress(done: int, total: int) -> None:
            test.frames_done, test.frames_total = done, total

        try:
            test.report = detect_people(
                input_path, self.video_path(test.id), test.tuning, default_model=self._default_model,
                people_factory=self._people_factory, max_seconds=test.max_seconds,
                on_progress=progress, should_stop=test.cancel.is_set)
            test.status = RunStatus.completed
        except Cancelled:
            pass
        except Exception as exc:
            log.warning("Person detection test %s failed", test.id, exc_info=True)
            test.status, test.error = RunStatus.failed, str(exc) or exc.__class__.__name__
        self._finish(test)

    def _finish(self, test: _Test) -> None:
        if test.cancel.is_set() or test.discard:
            with self._lock:
                self._tests.pop(test.id, None)
            shutil.rmtree(self.folder(test.id), ignore_errors=True)
        elif test.status == RunStatus.queued:
            test.status = RunStatus.failed

    def _prune(self) -> None:
        with self._lock:
            done = [t for t in self._tests.values() if t.status in (RunStatus.completed, RunStatus.failed)]
            for t in sorted(done, key=lambda t: t.created_at)[: max(0, len(done) - MAX_KEPT)]:
                del self._tests[t.id]
                shutil.rmtree(self.folder(t.id), ignore_errors=True)

    def delete(self, test_id: str) -> bool:
        with self._lock:
            test = self._tests.get(test_id)
            if test is None:
                return False
            if test.status in (RunStatus.queued, RunStatus.running):
                test.discard = True
                test.cancel.set()  # the worker removes it when it stops
                return True
            del self._tests[test_id]
        shutil.rmtree(self.folder(test_id), ignore_errors=True)
        return True

    def get(self, test_id: str) -> PeopleTestInfo | None:
        test = self._tests.get(test_id)
        return self.info(test) if test else None

    def list(self) -> list[PeopleTestInfo]:
        with self._lock:
            tests = sorted(self._tests.values(), key=lambda t: t.created_at, reverse=True)
        return [self.info(t) for t in tests if not t.discard]

    def info(self, t: _Test) -> PeopleTestInfo:
        total = max(t.frames_total, 1)
        return PeopleTestInfo(
            id=t.id, source=t.source, status=t.status, created_at=t.created_at, max_seconds=t.max_seconds,
            settings=t.report["settings"] if t.report else _settings_of(t.tuning),
            changes=[k for k, v in _settings_of(t.tuning).items() if v != _settings_of(Tuning())[k]],
            frames_done=t.frames_done, frames_total=t.frames_total,
            progress_fraction=1.0 if t.status == RunStatus.completed else min(1.0, t.frames_done / total),
            error=t.error, report=t.report)

    def shutdown(self) -> None:
        with self._lock:
            for t in self._tests.values():
                t.cancel.set()
        self._pool.shutdown(wait=False, cancel_futures=True)


def _settings_of(tuning: Tuning) -> dict:
    from ppe.tuning import PERSON_FIELDS

    return {k: getattr(tuning, k) for k in PERSON_FIELDS}
