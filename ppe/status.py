"""What the video shows for each tracked worker, voted over their recent PPE checks.

A single misread check shouldn't turn a box red for a whole interval, so an item is
shown as missing when it was missing in most of the worker's last few checks. On a
tie the previous state holds, so the box doesn't flip back and forth.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .analyzer import WorkerResult

VOTE_WINDOW = 3  # default; tunable as Tuning.vote_window


@dataclass
class PersonStatus:
    window: int = VOTE_WINDOW
    history: deque = field(init=False)  # violations per check, the last `window` checks
    shown: list[str] = field(default_factory=list)  # violation labels currently displayed
    unverified: list[str] = field(default_factory=list)  # from the latest check
    checks: int = 0
    last_check_frame: int | None = None

    def __post_init__(self):
        self.history = deque(maxlen=self.window)

    def add(self, result: WorkerResult, frame: int) -> None:
        if result.too_small:
            return  # no verdict; keep whatever was shown before
        self.history.append(list(result.violations))
        self.checks += 1
        self.last_check_frame = frame
        self.unverified = list(result.unverified)
        n = len(self.history)
        labels = list(dict.fromkeys(v for vs in self.history for v in vs))  # first-seen order
        shown = []
        for label in labels:
            count = sum(label in vs for vs in self.history)
            if 2 * count > n or (2 * count == n and label in self.shown):
                shown.append(label)
        self.shown = shown

    def display(self, track_id: int, box, too_small: bool) -> WorkerResult:
        """The WorkerResult to draw for this worker in the current frame."""
        if not self.checks:
            return WorkerResult(box=box, track_id=track_id, too_small=too_small, pending=not too_small)
        return WorkerResult(box=box, track_id=track_id, violations=list(self.shown),
                            unverified=list(self.unverified))
