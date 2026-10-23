"""Dashboard metrics derived from a run's per-frame timeline."""

from __future__ import annotations

from collections import Counter

from ppe.config import PPE_CATALOG

from ..schemas.runs import PersonSummary, RunSummary, TimelinePoint, TypeCount, ViolationEvent


def snapshot_name(frame: int, worker_index: int) -> str:
    return f"{frame:06d}_{worker_index}.jpg"


def frame_name(frame: int) -> str:
    return f"{frame:06d}.jpg"


def person_photo_name(track_id: int) -> str:
    return f"{track_id}.jpg"


def status_label(violations: list[str]) -> str:
    return "PPE OK" if not violations else f"PPE Violation ({len(violations)}) - {', '.join(violations)}"


def build_people(timeline: list[dict], available_photos: set[str] | None = None,
                 flag_ratio: float = 0.5) -> list[PersonSummary]:
    """Aggregate each tracked person's checks.

    A person's overall status flags an item only when it was missing in more than
    `flag_ratio` of their checks (default half), so a single misread frame doesn't
    mark them as violating.
    """
    people: dict[int, dict] = {}
    for entry in timeline:
        for w in entry["workers"]:
            tid = w.get("track_id")
            if tid is None or w.get("too_small"):
                continue
            p = people.setdefault(tid, {"first": entry["time_sec"], "checks": 0, "bad": 0,
                                        "missing": Counter(), "latest": []})
            p["last"] = entry["time_sec"]
            p["checks"] += 1
            p["bad"] += bool(w["violations"])
            p["missing"].update(w["violations"])
            p["latest"] = w["violations"]

    out = []
    for tid, p in sorted(people.items()):
        overall = [label for label, n in p["missing"].most_common() if n > p["checks"] * flag_ratio]
        photo = person_photo_name(tid)
        out.append(PersonSummary(
            id=tid,
            first_seen_sec=p["first"],
            last_seen_sec=p["last"],
            checks=p["checks"],
            violation_checks=p["bad"],
            ok=not overall,
            status_label=status_label(overall),
            missing=[TypeCount(label=k, count=v) for k, v in p["missing"].most_common()],
            latest_label=status_label(p["latest"]),
            photo=photo if available_photos is None or photo in available_photos else None,
        ))
    # Violating people first, then by id.
    return sorted(out, key=lambda x: (x.ok, x.id))


def build_summary(timeline: list[dict], available_snapshots: set[str] | None = None,
                  available_frames: set[str] | None = None,
                  available_photos: set[str] | None = None, flag_ratio: float = 0.5) -> RunSummary:
    """available_* limit image references to files that exist (None = assume all exist)."""
    by_type: Counter[str] = Counter()
    unverified: Counter[str] = Counter()
    points, events = [], []
    worker_checks = violation_checks = frames_with_violation = peak = too_small = 0

    regular = 0
    for entry in timeline:
        workers = entry["workers"]
        # Arrival checks hold only newcomers: they count as checks, but aren't a point in time
        # with everyone on screen, so they stay out of the timeline and per-frame figures.
        arrival = entry.get("arrival", False)
        n_bad = 0
        for i, w in enumerate(workers):
            if w.get("too_small"):
                too_small += 1
                continue
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
                track_id=w.get("track_id"),
                violations=w["violations"],
                box=list(w["box"]),
                snapshot=snap if available_snapshots is None or snap in available_snapshots else None,
                frame_image=(frame_name(entry["frame"])
                             if available_frames is None or frame_name(entry["frame"]) in available_frames else None),
            ))
        worker_checks += sum(not w.get("too_small") for w in workers)
        violation_checks += n_bad
        if arrival:
            continue
        regular += 1
        frames_with_violation += bool(n_bad)
        peak = max(peak, len(workers))
        points.append(TimelinePoint(time_sec=entry["time_sec"], workers=len(workers), violations=n_bad,
                                    too_far=sum(bool(w.get("too_small")) for w in workers)))

    n = regular
    return RunSummary(
        frames_analysed=n,
        arrival_checks=len(timeline) - n,
        peak_workers=peak,
        avg_workers=round(sum(p.workers for p in points) / n, 2) if n else 0.0,
        worker_checks=worker_checks,
        ok_checks=worker_checks - violation_checks,
        violation_checks=violation_checks,
        compliance_rate=round((worker_checks - violation_checks) / worker_checks, 4) if worker_checks else None,
        frames_with_violation=frames_with_violation,
        by_type=[TypeCount(label=k, count=v) for k, v in by_type.most_common()],
        unverified=[TypeCount(label=k, count=v) for k, v in unverified.most_common()],
        too_small_checks=too_small,
        timeline=points,
        events=events,
        tracked=any(w.get("track_id") is not None for e in timeline for w in e["workers"]),
        people=build_people(timeline, available_photos, flag_ratio),
    )
