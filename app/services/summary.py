"""Dashboard metrics derived from a run's per-frame timeline.

Everything here uses the same voted status the video shows (stored with each check by
ppe.video; replayed for runs made before it was stored), so the video, the violation
list and the People section agree:
  - a check counts as a violation when the worker's status after it shows a missing item;
  - a violation *incident* is a stretch of consecutive checks of one person with a violation;
  - a person is flagged for an item when they had an incident for it.
"""

from __future__ import annotations

from collections import Counter

from ppe.analyzer import WorkerResult, item_name
from ppe.status import CONFIRM_CHECKS, VOTE_WINDOW, PersonStatus

from ..schemas.runs import PersonSummary, RunSummary, TimelinePoint, TypeCount, ViolationEvent

VERDICTS = {"ok", "violation", "unverified", "pending"}  # states of a check that reached Moondream


def snapshot_name(frame: int, worker_index: int) -> str:
    return f"{frame:06d}_{worker_index}.jpg"


def frame_name(frame: int) -> str:
    return f"{frame:06d}.jpg"


def person_photo_name(track_id: int) -> str:
    return f"{track_id}.jpg"


def status_label(violations: list[str]) -> str:
    return "PPE OK" if not violations else f"PPE Violation ({len(violations)}) - {', '.join(violations)}"


def with_status(timeline: list[dict], window: int = VOTE_WINDOW, confirm: int = CONFIRM_CHECKS) -> list[dict]:
    """The timeline with every worker's voted status. Runs made before it was stored get it
    replayed from their raw checks; workers without a track id (older runs) use their raw check."""
    if all("shown" in w for e in timeline for w in e["workers"] if w.get("track_id") is not None):
        return timeline
    statuses: dict[int, PersonStatus] = {}
    out = []
    for e in timeline:
        workers = []
        for w in e["workers"]:
            tid = w.get("track_id")
            if tid is None:
                workers.append({**w, "confirmed": True, "shown": w["violations"],
                                "shown_unverified": w.get("unverified", [])})
                continue
            s = statuses.setdefault(tid, PersonStatus(window=window, confirm=confirm))
            if not w.get("too_small"):
                s.add(WorkerResult(box=tuple(w["box"]), violations=w["violations"],
                                   unverified=w.get("unverified", []),
                                   check_failed=w.get("check_failed", False)), e["frame"])
            workers.append({**w, "confirmed": s.confirmed, "shown": list(s.shown),
                            "shown_unverified": list(s.unverified)})
        out.append({**e, "workers": workers})
    return out


def check_state(w: dict) -> str:
    """too_far, failed, new (on screen, not checked yet), pending (checked, no verdict yet),
    violation, unverified or ok."""
    if w.get("too_small"):
        return "too_far"
    if w.get("check_failed"):
        return "failed"
    if w.get("pending"):
        return "new"
    if not w.get("confirmed", True):
        return "pending"
    # Workers without a stored status (runs made before tracking) use their raw check.
    if w.get("shown", w.get("violations")):
        return "violation"
    if w.get("shown_unverified", w.get("unverified")):
        return "unverified"
    return "ok"


def _incidents(timeline: list[dict], available_snapshots, available_frames) -> list[ViolationEvent]:
    """Consecutive violation checks of the same person, merged into one incident each."""
    def snap(entry, i):
        name = snapshot_name(entry["frame"], i)
        return name if available_snapshots is None or name in available_snapshots else None

    def frame_image(entry):
        name = frame_name(entry["frame"])
        return name if available_frames is None or name in available_frames else None

    events: list[ViolationEvent] = []
    open_by_person: dict[int, dict] = {}

    def close(tid):
        inc = open_by_person.pop(tid, None)
        if inc is None:
            return
        best = inc["best"]  # the check with the most raw violations: its photo shows the problem best
        events.append(ViolationEvent(
            time_sec=inc["start"], end_sec=inc["end"], checks=inc["checks"], frame=best[0]["frame"],
            track_id=tid, violations=inc["labels"], box=list(best[1]["box"]),
            snapshot=snap(best[0], best[2]) if best[1]["violations"] else None,
            frame_image=frame_image(best[0])))

    for entry in timeline:
        for i, w in enumerate(entry["workers"]):
            state = check_state(w)
            tid = w.get("track_id")
            if tid is None:
                # Runs made before tracking: one card per violating check.
                if w["violations"] and state == "violation":
                    events.append(ViolationEvent(time_sec=entry["time_sec"], frame=entry["frame"],
                                                 violations=w["violations"], box=list(w["box"]),
                                                 snapshot=snap(entry, i), frame_image=frame_image(entry)))
                continue
            if state not in VERDICTS:
                continue  # too far, failed or not checked: says nothing either way
            if state != "violation":
                close(tid)
                continue
            inc = open_by_person.setdefault(tid, {"start": entry["time_sec"], "checks": 0, "labels": [],
                                                  "best": (entry, w, i)})
            inc["end"] = entry["time_sec"]
            inc["checks"] += 1
            inc["labels"] += [v for v in w.get("shown", w["violations"]) if v not in inc["labels"]]
            if len(w["violations"]) > len(inc["best"][1]["violations"]):
                inc["best"] = (entry, w, i)
    for tid in list(open_by_person):
        close(tid)
    return sorted(events, key=lambda e: (e.time_sec, e.track_id or 0))


def build_people(timeline: list[dict], available_photos: set[str] | None = None,
                 incidents: list[ViolationEvent] | None = None) -> list[PersonSummary]:
    """Each tracked person over the whole video.

    Flagged for an item when they had a violation incident for it (their status showed it
    missing, i.e. it was missing in at least `confirm_checks` recent checks). "Seen briefly"
    when they were never checked often enough for a verdict; "Not verified" when an item
    couldn't be seen in most of their checks.
    """
    timeline = with_status(timeline)
    if incidents is None:
        incidents = _incidents(timeline, None, None)
    people: dict[int, dict] = {}
    for entry in timeline:
        for w in entry["workers"]:
            tid = w.get("track_id")
            state = check_state(w)
            if tid is None or state in ("too_far", "new"):
                continue
            p = people.setdefault(tid, {"first": entry["time_sec"], "checks": 0, "bad": 0, "unverified": 0,
                                        "failed": 0, "confirmed": False, "missing": Counter(), "latest": []})
            p["last"] = entry["time_sec"]
            if state == "failed":
                p["failed"] += 1
                continue
            p["checks"] += 1
            p["bad"] += bool(w["violations"])
            p["unverified"] += bool(w.get("unverified"))
            p["missing"].update(w["violations"])
            p["confirmed"] = p["confirmed"] or w.get("confirmed", True)
            p["latest"] = w.get("shown", w["violations"])

    by_person: dict[int, list[ViolationEvent]] = {}
    for e in incidents:
        by_person.setdefault(e.track_id, []).append(e)

    out = []
    for tid, p in sorted(people.items()):
        inc = by_person.get(tid, [])
        flagged = list(dict.fromkeys(v for e in inc for v in e.violations))
        if flagged:
            state, label = "flagged", status_label(flagged)
        elif not p["confirmed"]:
            state, label = "brief", "Seen briefly"
        elif 2 * p["unverified"] > p["checks"]:
            state, label = "unverified", "Not verified"
        else:
            state, label = "ok", "PPE OK"
        photo = person_photo_name(tid)
        out.append(PersonSummary(
            id=tid,
            first_seen_sec=p["first"],
            last_seen_sec=p["last"],
            checks=p["checks"],
            violation_checks=p["bad"],
            unverified_checks=p["unverified"],
            failed_checks=p["failed"],
            incidents=len(inc),
            violation_sec=round(sum(e.end_sec - e.time_sec for e in inc), 2),
            state=state,
            ok=state == "ok",
            status_label=label,
            missing=[TypeCount(label=k, count=v) for k, v in p["missing"].most_common()],
            latest_label=status_label(p["latest"]),
            photo=photo if available_photos is None or photo in available_photos else None,
        ))
    # Flagged people first, then by id.
    order = {"flagged": 0, "unverified": 1, "brief": 2, "ok": 3}
    return sorted(out, key=lambda x: (order[x.state], x.id))


def build_summary(timeline: list[dict], available_snapshots: set[str] | None = None,
                  available_frames: set[str] | None = None,
                  available_photos: set[str] | None = None,
                  vote_window: int = VOTE_WINDOW, confirm_checks: int = CONFIRM_CHECKS) -> RunSummary:
    """available_* limit image references to files that exist (None = assume all exist).
    vote_window / confirm_checks replay the video's vote for runs made before it was stored."""
    timeline = with_status(timeline, vote_window, confirm_checks)
    states: Counter[str] = Counter()
    unverified: Counter[str] = Counter()
    points = []
    frames_with_violation = peak = regular = 0

    for entry in timeline:
        workers = entry["workers"]
        here = Counter(check_state(w) for w in workers)
        for w in workers:
            if check_state(w) in VERDICTS:
                for item in w.get("unverified", []):
                    unverified[item_name(item)] += 1
        # Placeholders for workers too new to check aren't checks.
        states.update({k: v for k, v in here.items() if k != "new"})
        # Arrival checks hold only newcomers: they count as checks, but aren't a point in time
        # with everyone on screen, so they stay out of the timeline and per-frame figures.
        if entry.get("arrival", False):
            continue
        regular += 1
        frames_with_violation += bool(here["violation"])
        peak = max(peak, len(workers))
        points.append(TimelinePoint(time_sec=entry["time_sec"], workers=len(workers),
                                    violations=here["violation"], too_far=here["too_far"],
                                    unverified=here["unverified"],
                                    unchecked=here["new"] + here["pending"] + here["failed"]))

    events = _incidents(timeline, available_snapshots, available_frames)
    by_type: Counter[str] = Counter(v for e in events for v in e.violations)
    worker_checks = sum(states[s] for s in VERDICTS)
    judged = states["ok"] + states["violation"]
    return RunSummary(
        frames_analysed=regular,
        arrival_checks=len(timeline) - regular,
        peak_workers=peak,
        avg_workers=round(sum(p.workers for p in points) / regular, 2) if regular else 0.0,
        worker_checks=worker_checks,
        ok_checks=states["ok"],
        violation_checks=states["violation"],
        unverified_checks=states["unverified"],
        pending_checks=states["pending"],
        failed_checks=states["failed"],
        compliance_rate=round(states["ok"] / judged, 4) if judged else None,
        frames_with_violation=frames_with_violation,
        by_type=[TypeCount(label=k, count=v) for k, v in by_type.most_common()],
        unverified=[TypeCount(label=k, count=v) for k, v in unverified.most_common()],
        too_small_checks=states["too_far"],
        timeline=points,
        events=events,
        tracked=any(w.get("track_id") is not None for e in timeline for w in e["workers"]),
        people=build_people(timeline, available_photos, events),
    )
