"""Developer settings (ppe.tuning.Tuning) applied to new runs, saved on the server.

Stored at <data_dir>/_dev/tuning.json. A missing or unreadable file means defaults.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from pydantic import ValidationError

from ppe.tuning import Tuning

from .run_store import replace_with_retry

log = logging.getLogger(__name__)


class DevSettingsStore:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "_dev" / "tuning.json"
        self._lock = threading.Lock()

    def get(self) -> Tuning:
        with self._lock:
            if not self.path.exists():
                return Tuning()
            try:
                return Tuning.model_validate_json(self.path.read_text())
            except (ValidationError, ValueError):
                log.warning("Ignoring unreadable developer settings at %s", self.path, exc_info=True)
                return Tuning()

    def save(self, tuning: Tuning) -> Tuning:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(tuning.model_dump(), indent=2))
            replace_with_retry(tmp, self.path)
        return tuning

    def reset(self) -> Tuning:
        with self._lock:
            self.path.unlink(missing_ok=True)
        return Tuning()
