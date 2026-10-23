"""What the video shows for each tracked worker, voted over their recent PPE checks.

A single misread check shouldn't turn a box red, so an item is shown as missing when it
was missing in most of the worker's last few checks, and in at least `confirm` of them.
Until a worker has `confirm` checks they show as "Checking...". On a tie the previous
state holds, so the box doesn't flip back and forth. "Unseen" items are voted the same
way (without the minimum), and checks that failed are left out.

The same status is written to the report for every check (see ppe.video), so the video,
the violation list and the People section all agree.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .analyzer import WorkerResult

VOTE_WINDOW = 3  # default; tunable as Tuning.vote_window
CONFIRM_CHECKS = 2  # default; tunable as Tuning.confirm_checks


def vote(history, previous: list[str], minimum: int = 1) -> list[str]:
    """Labels missing in more than half of `history` (and at least `minimum` times); ties keep `previous`."""
    n = len(history)
    labels = list(dict.fromkeys(v for vs in history for v in vs))  # first-seen order
    shown = []
    for label in labels:
        count = sum(label in vs for vs in history)
        if count >= minimum and (2 * count > n or (2 * count == n and label in previous)):
            shown.append(label)
    return shown


@dataclass
class PersonStatus:
    window: int = VOTE_WINDOW
    confirm: int = CONFIRM_CHECKS
    history: deque = field(init=False)  # violations per check, the last `window` checks
    unverified_history: deque = field(init=False)
    shown: list[str] = field(default_factory=list)  # violation labels currently displayed
    unverified: list[str] = field(default_factory=list)  # unseen items currently displayed
    checks: int = 0
    last_check_frame: int | None = None

    def __post_init__(self):
        self.history = deque(maxlen=self.window)
        self.unverified_history = deque(maxlen=self.window)

    @property
    def confirmed(self) -> bool:
        """Enough checks for a verdict."""
        return self.checks >= min(self.confirm, self.window)

    def add(self, result: WorkerResult, frame: int) -> None:
        if result.too_small or result.check_failed:
            return  # no verdict; keep whatever was shown before
        self.history.append(list(result.violations))
        self.unverified_history.append(list(result.unverified))
        self.checks += 1
        self.last_check_frame = frame
        self.shown = vote(self.history, self.shown, min(self.confirm, self.window))
        self.unverified = vote(self.unverified_history, self.unverified)

    def label(self) -> str:
        """Short status, e.g. "PPE OK" or "Missing: Helmet"."""
        if self.shown:
            return "Missing: " + ", ".join(v.removeprefix("No ") for v in self.shown)
        return "PPE OK"

    def display(self, track_id: int, box, too_small: bool) -> WorkerResult:
        """The WorkerResult to draw for this worker in the current frame."""
        if too_small:
            # Too far to check now; say what they were when last checked, in grey.
            return WorkerResult(box=box, track_id=track_id, too_small=True,
                                last_status=self.label() if self.confirmed else None)
        if not self.confirmed:
            return WorkerResult(box=box, track_id=track_id, pending=True)
        return WorkerResult(box=box, track_id=track_id, violations=list(self.shown),
                            unverified=list(self.unverified))
