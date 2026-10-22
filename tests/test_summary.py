from app.services.summary import build_people


def entry(t, *workers):
    return {"frame": int(t * 30), "time_sec": t, "workers": [
        {"track_id": tid, "box": [0, 0, 10, 10], "violations": v, "unverified": []} for tid, v in workers]}


def test_person_status_uses_majority_of_their_checks():
    timeline = [
        entry(0, (1, ["No Helmet"]), (2, ["No Gloves"])),  # one-off misread for person 1
        entry(1, (1, []), (2, ["No Gloves"])),
        entry(2, (1, []), (2, [])),
    ]
    people = {p.id: p for p in build_people(timeline, available_photos=set())}
    assert people[1].ok and people[1].status_label == "PPE OK"
    assert people[1].missing[0].label == "No Helmet" and people[1].missing[0].count == 1
    assert people[2].status_label == "PPE Violation (1) - No Gloves"
    assert (people[2].first_seen_sec, people[2].last_seen_sec, people[2].checks) == (0, 2, 3)
    assert people[2].latest_label == "PPE OK"


def test_too_far_workers_are_their_own_part_of_the_timeline():
    from app.services.summary import build_summary

    timeline = [{"frame": 0, "time_sec": 0, "workers": [
        {"track_id": 1, "box": [0, 0, 10, 10], "violations": [], "unverified": []},
        {"track_id": 2, "box": [0, 0, 10, 10], "violations": ["No Vest"], "unverified": []},
        {"track_id": 3, "box": [0, 0, 1, 1], "violations": [], "unverified": [], "too_small": True},
    ]}]
    s = build_summary(timeline)
    [p] = s.timeline
    assert (p.workers, p.violations, p.too_far) == (3, 1, 1)  # so PPE OK = 1, not 2
    assert (s.worker_checks, s.ok_checks, s.too_small_checks) == (2, 1, 1)
