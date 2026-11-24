"""Developer settings (ppe.tuning.Tuning) applied to new runs, saved on the server.

There are two settings states, each with its own saved settings: state 1 (the initial prompt and its
double-checks) and state 2 (the current prompt, which asks whether the body part is visible). One of
them is active; new runs use it. Stored at <data_dir>/_dev/tuning.json as
{"active": "2", "states": {"1": {...}, "2": {...}}}. A state with nothing saved uses its defaults, and a
missing or unreadable file means state 2's defaults. Files saved before there were two states hold
one Tuning: it becomes state 2's.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from pydantic import ValidationError

from ppe.tuning import SETTINGS_STATES, Tuning

from .run_store import replace_with_retry

log = logging.getLogger(__name__)

DEFAULT_STATE = "2"


class DevSettingsStore:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "_dev" / "tuning.json"
        self._lock = threading.Lock()

    # -- file ----------------------------------------------------------------
    def _read(self) -> dict:
        """{"active": state, "states": {state: settings dict}} (only the states that were saved)."""
        if not self.path.exists():
            return {"active": DEFAULT_STATE, "states": {}}
        try:
            raw = json.loads(self.path.read_text())
            if not isinstance(raw, dict):
                raise ValueError("not an object")
            if "states" not in raw:  # saved before there were two states
                return {"active": DEFAULT_STATE, "states": {DEFAULT_STATE: raw}}
            active = raw.get("active") if raw.get("active") in SETTINGS_STATES else DEFAULT_STATE
            return {"active": active, "states": dict(raw["states"])}
        except (ValueError, OSError):
            log.warning("Ignoring unreadable developer settings at %s", self.path, exc_info=True)
            return {"active": DEFAULT_STATE, "states": {}}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        replace_with_retry(tmp, self.path)

    @staticmethod
    def _tuning(data: dict, state: str) -> Tuning:
        saved = data["states"].get(state)
        if saved is None:
            return Tuning.for_state(state)
        try:
            return Tuning.model_validate({**saved, "settings_state": state})
        except (ValidationError, ValueError):
            log.warning("Ignoring unreadable developer settings for state %s", state, exc_info=True)
            return Tuning.for_state(state)

    # -- API -----------------------------------------------------------------
    def active_state(self) -> str:
        with self._lock:
            return self._read()["active"]

    def get(self, state: str | None = None) -> Tuning:
        """The settings of `state`, default: the active state (the one new runs use)."""
        with self._lock:
            data = self._read()
            return self._tuning(data, state or data["active"])

    def save(self, tuning: Tuning) -> Tuning:
        """Saves `tuning` as the settings of its own state, and makes that state the active one."""
        with self._lock:
            data = self._read()
            data["states"][tuning.settings_state] = tuning.model_dump()
            data["active"] = tuning.settings_state
            self._write(data)
        return tuning

    def set_active(self, state: str) -> Tuning:
        """Switches the state new runs use; each state keeps its own saved settings."""
        if state not in SETTINGS_STATES:
            raise ValueError(f"unknown settings state {state!r}")
        with self._lock:
            data = self._read()
            data["active"] = state
            self._write(data)
            return self._tuning(data, state)

    def reset(self) -> Tuning:
        """Back to the defaults of the active state (the other state's saved settings stay)."""
        with self._lock:
            data = self._read()
            state = data["active"]
            data["states"].pop(state, None)
            self._write(data)
        return Tuning.for_state(state)
