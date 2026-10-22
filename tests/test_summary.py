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
