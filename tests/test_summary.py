from app.services.summary import build_people, build_summary
from ppe.analyzer import WorkerResult
from ppe.status import PersonStatus


def worker(tid, violations=(), unverified=(), **extra):
    return {"track_id": tid, "box": [0, 0, 10, 10], "violations": list(violations),
            "unverified": list(unverified), **extra}


def entry(t, *workers, arrival=False):
    return {"frame": int(t * 10), "time_sec": t, "arrival": arrival, "workers": list(workers)}


def run(pattern, tid=1):
    """Timeline from raw checks, M = missing a helmet, U = helmet unseen, O = all PPE (no stored vote)."""
    return [entry(float(i), worker(tid, ["No Helmet"] if c == "M" else [], ["helmet"] if c == "U" else []))
            for i, c in enumerate(pattern)]


def video_red(pattern):
    """How many checks the video shows red for the same raw checks."""
    s, red = PersonStatus(), 0
    for i, c in enumerate(pattern):
        s.add(WorkerResult((0, 0, 1, 1), ["No Helmet"] if c == "M" else []), i)
        red += bool(s.display(1, (0, 0, 1, 1), False).violations)
    return red


def test_long_violation_flags_the_person_even_if_it_is_a_minority_of_their_time():
    # Helmet on for 60 s, then off for 50 s: the old "more than half of all checks" rule said PPE OK.
    pattern = "O" * 60 + "M" * 50
    s = build_summary(run(pattern))
    [p] = s.people
    assert p.state == "flagged" and p.status_label == "PPE Violation (1) - No Helmet"
    [inc] = s.events  # one incident, not 50 cards
    assert (inc.time_sec, inc.end_sec, inc.checks) == (61.0, 109.0, 49)
    assert p.violation_sec == 48.0 and p.incidents == 1


def test_video_list_and_people_agree():
    for pattern in ["MO", "MOOOO", "OOMOO", "OOMMMM", "MMMOOO", "MOMOMO", "M", "OOOOOOOMMM" * 6]:
        s = build_summary(run(pattern))
        red = video_red(pattern)
        assert s.violation_checks == red, pattern
        assert sum(e.checks for e in s.events) == red, pattern
        assert (s.people[0].state == "flagged") == (red > 0), pattern


def test_single_misread_is_never_a_violation():
    s = build_summary(run("MOOOO"))
    assert s.violation_checks == 0 and s.events == [] and s.people[0].state == "ok"
    assert s.people[0].violation_checks == 1  # still visible as a raw check


def test_one_check_is_seen_briefly():
    [p] = build_people(run("M"), available_photos=set())
    assert p.state == "brief" and p.status_label == "Seen briefly" and not p.ok


def test_unverified_is_not_compliance():
    s = build_summary(run("UUUU"))
    assert s.compliance_rate is None  # no check could judge the helmet
    assert (s.ok_checks, s.unverified_checks, s.pending_checks) == (0, 3, 1)
    assert s.people[0].state == "unverified" and s.people[0].status_label == "Not verified"
    mixed = build_summary(run("OOUU"))
    assert mixed.compliance_rate == 1.0 and mixed.unverified_checks == 1  # voted: unseen in 2 of the last 3


def test_stored_vote_is_used_as_is():
    # The pipeline stores the voted status; the summary must not vote again.
    tl = [entry(0, worker(1, ["No Vest"], confirmed=True, shown=["No Vest"], shown_unverified=[]))]
    s = build_summary(tl)
    assert s.violation_checks == 1 and s.events[0].violations == ["No Vest"]


def test_failed_and_new_workers_are_not_checks():
    tl = [entry(0, worker(1, check_failed=True, failed=["helmet"]), worker(2, pending=True),
                worker(3, too_small=True))]
    s = build_summary(tl)
    assert (s.worker_checks, s.failed_checks, s.too_small_checks) == (0, 1, 1)
    [p] = s.timeline
    assert (p.workers, p.violations, p.too_far, p.unchecked) == (3, 0, 1, 2)
    assert [x.id for x in s.people] == [1] and s.people[0].failed_checks == 1


def test_too_far_workers_are_their_own_part_of_the_timeline():
    timeline = [entry(0, worker(1), worker(2, ["No Vest"]), worker(3, too_small=True))]
    s = build_summary(timeline, confirm_checks=1)
    [p] = s.timeline
    assert (p.workers, p.violations, p.too_far) == (3, 1, 1)  # so PPE OK = 1, not 2
    assert (s.worker_checks, s.ok_checks, s.too_small_checks) == (2, 1, 1)


def test_runs_made_before_tracking_use_their_raw_checks():
    tl = [{"frame": 0, "time_sec": 0, "workers": [
        {"box": [0, 0, 10, 10], "violations": ["No Vest"], "unverified": []},
        {"box": [0, 0, 10, 10], "violations": [], "unverified": []}]}]
    s = build_summary(tl)
    assert (s.violation_checks, s.ok_checks, len(s.events)) == (1, 1, 1) and not s.tracked


def test_incident_records_when_it_was_detected_and_whether_it_was_resolved():
    timeline = run("OOMMMMOO") + [entry(8.0 + i, worker(2, ["No Helmet"])) for i in range(4)]
    for i, e in enumerate(timeline):
        e["detected_at"] = f"2026-10-24T10:00:{i:02d}+00:00"
    put_back_on, still_missing = build_summary(timeline).events
    assert put_back_on.track_id == 1 and put_back_on.resolved is True
    assert put_back_on.detected_at.isoformat() == "2026-10-24T10:00:03+00:00"  # first check shown red
    assert still_missing.track_id == 2 and still_missing.resolved is False
    assert build_summary(run("OOMMMM")).events[0].detected_at is None  # runs made before it was recorded


def test_violation_links_the_frame_without_boxes_when_it_was_saved():
    timeline = run("MMMM")
    [with_clean] = build_summary(timeline, available_frames={"000010.jpg", "000010_clean.jpg"}).events
    assert (with_clean.frame_image, with_clean.clean_image) == ("000010.jpg", "000010_clean.jpg")
    [older] = build_summary(timeline, available_frames={"000010.jpg"}).events
    assert older.clean_image is None
