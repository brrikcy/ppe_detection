"""Dashboard metrics derived from a run's per-frame timeline."""

from __future__ import annotations

from collections import Counter

from ppe.config import PPE_CATALOG

from ..schemas.runs import RunSummary, TimelinePoint, TypeCount, ViolationEvent


def snapshot_name(frame: int, worker_index: int) -> str:
    return f"{frame:06d}_{worker_index}.jpg"


def build_summary(timeline: list[dict], available_snapshots: set[str] | None = None) -> RunSummary:
    by_type: Counter[str] = Counter()
    unverified: Counter[str] = Counter()
    points, events = [], []
    worker_checks = violation_checks = frames_with_violation = peak = 0

    for entry in timeline:
        workers = entry["workers"]
        n_bad = 0
        for i, w in enumerate(workers):
            for item in w.get("unverified", []):
                unverified[PPE_CATALOG[item].label.removeprefix("No ") if item in PPE_CATALOG else item] += 1
            if not w["violations"]:
                continue
            n_bad += 1
            by_type.update(w["violations"])
            snap = snapshot_name(entry["frame"], i)
            events.append(ViolationEvent(
                time_sec=entry["time_sec"],
                frame=entry["frame"],
                violations=w["violations"],
                box=list(w["box"]),
                snapshot=snap if available_snapshots is None or snap in available_snapshots else None,
            ))
        worker_checks += len(workers)
        violation_checks += n_bad
        frames_with_violation += bool(n_bad)
        peak = max(peak, len(workers))
        points.append(TimelinePoint(time_sec=entry["time_sec"], workers=len(workers), violations=n_bad))

    n = len(timeline)
    return RunSummary(
        frames_analysed=n,
        peak_workers=peak,
        avg_workers=round(worker_checks / n, 2) if n else 0.0,
        worker_checks=worker_checks,
        ok_checks=worker_checks - violation_checks,
        violation_checks=violation_checks,
        compliance_rate=round((worker_checks - violation_checks) / worker_checks, 4) if worker_checks else None,
        frames_with_violation=frames_with_violation,
        by_type=[TypeCount(label=k, count=v) for k, v in by_type.most_common()],
        unverified=[TypeCount(label=k, count=v) for k, v in unverified.most_common()],
        timeline=points,
        events=events,
    )
