"""The person detection playground: person-only tests with their own settings."""

import time

import pytest
from fastapi.testclient import TestClient

from app.core.settings import Settings
from app.main import create_app
from ppe.people_playground import Cancelled, detect_people
from ppe.tuning import PERSON_FIELDS, Tuning, with_person_settings

from .test_api import _video
from .test_pipeline import LEFT, RIGHT, FakePeople, write_video


def _tuning(**kw):
    return with_person_settings({"min_check_height_px": 0, "min_check_height_frac": 0, "min_check_age_sec": 0, **kw})


def test_report_counts_people_and_who_would_be_checked(tmp_path):
    src = write_video(tmp_path / "in.mp4", frames=20, fps=10)
    # #2 appears at frame 10 and is gone at 12: a 0.2 s flicker.
    people = FakePeople(lambda i: [(1, LEFT)] + ([(2, RIGHT)] if 10 <= i < 12 else []))
    seen = []
    report = detect_people(src, tmp_path / "out.mp4", _tuning(), people_factory=lambda fps: people,
                           on_progress=lambda d, t: seen.append((d, t)))
    assert (tmp_path / "out.mp4").exists()
    assert report["frames"] == 20 and report["people"] == 2 and report["max_per_frame"] == 2
    assert report["short_tracks"] == 1  # the flicker, not #1
    assert [t["id"] for t in report["tracks"]] == [1, 2]
    assert report["tracks"][1]["short"] is True and report["tracks"][0]["short"] is False
    assert report["would_be_checked"] == 2 and report["never_checked"] == 0
    assert report["frames_without_people"] == 0
    assert report["settings"] == {k: getattr(_tuning(), k) for k in PERSON_FIELDS}
    assert seen and seen[-1] == (20, 20)


def test_small_or_new_people_are_not_checked(tmp_path):
    src = write_video(tmp_path / "in.mp4", frames=20, fps=10)
    # LEFT is 150 px tall in this 200 px video.
    small = detect_people(src, tmp_path / "a.mp4", _tuning(min_check_height_px=200),
                          people_factory=lambda fps: FakePeople(lambda i: [(1, LEFT)]))
    assert small["would_be_checked"] == 0 and small["never_checked"] == 1
    assert small["tracks"][0]["checked"] is False
    # A person who arrives late has to be on screen for min_check_age_sec first.
    late = detect_people(src, tmp_path / "b.mp4", _tuning(min_check_age_sec=5),
                         people_factory=lambda fps: FakePeople(lambda i: [(1, LEFT)] if i >= 2 else []))
    assert late["never_checked"] == 1 and late["frames_without_people"] == 2
    # ...but someone there from the first frame is established.
    first = detect_people(src, tmp_path / "c.mp4", _tuning(min_check_age_sec=5),
                          people_factory=lambda fps: FakePeople(lambda i: [(1, LEFT)]))
    assert first["would_be_checked"] == 1


def test_max_seconds_and_cancel(tmp_path):
    src = write_video(tmp_path / "in.mp4", frames=30, fps=10)
    report = detect_people(src, tmp_path / "out.mp4", _tuning(), people_factory=lambda fps: FakePeople(),
                           max_seconds=1)
    assert report["frames"] == 10
    with pytest.raises(Cancelled):
        detect_people(src, tmp_path / "x.mp4", _tuning(), people_factory=lambda fps: FakePeople(),
                      should_stop=lambda: True)
    assert not (tmp_path / "x.mp4").exists()


def test_with_person_settings_validates():
    assert with_person_settings({"detector_conf": 0.3}).detector_conf == 0.3
    assert with_person_settings({}) == Tuning()
    with pytest.raises(ValueError):
        with_person_settings({"detector_imgsz": 650})  # not a multiple of 32
    with pytest.raises(ValueError):
        with_person_settings({"question_template": "x {item}"})  # not a person detection setting


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(data_dir=tmp_path / "runs", server_api_key=None),
                     people_factory=lambda fps: FakePeople())
    with TestClient(app) as c:
        yield c


def _wait(client, test_id, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/dev/people/{test_id}").json()
        if body["status"] in ("completed", "failed"):
            return body
        time.sleep(0.1)
    raise AssertionError("test did not finish")


def _start(client, tmp_path, settings="{}", **data):
    src = _video(tmp_path / "site.mp4")
    with src.open("rb") as f:
        return client.post("/api/dev/people", files={"video": ("site.mp4", f, "video/mp4")},
                           data={"person_settings": settings, **data})


def test_test_runs_with_its_own_settings_and_leaves_the_saved_ones_alone(client, tmp_path):
    saved = client.get("/api/dev/tuning").json()["active"]
    r = _start(client, tmp_path, '{"detector_conf": 0.4, "min_check_height_px": 0}')
    assert r.status_code == 201
    body = _wait(client, r.json()["id"])
    assert body["status"] == "completed", body["error"]
    assert body["settings"]["detector_conf"] == 0.4
    assert body["changes"] == ["detector_conf", "min_check_height_px"]
    assert body["report"]["people"] == 2 and body["progress_fraction"] == 1.0
    assert body["source"] == "site.mp4"
    video = client.get(f"/api/dev/people/{body['id']}/video")
    assert video.status_code == 200 and video.headers["content-type"] == "video/mp4"
    assert client.get("/api/dev/tuning").json()["active"] == saved  # nothing saved
    assert [t["id"] for t in client.get("/api/dev/people").json()] == [body["id"]]


def test_test_from_a_run_video(client, tmp_path):
    from datetime import datetime, timezone

    from app.schemas.runs import KeySource, RunMeta, RunOptions, VideoInfo

    store = client.app.state.store
    paths = store.create_dir("r1")
    _video(paths.root / "input.mp4")
    store.save(RunMeta(id="r1", name="Gate cam", created_at=datetime.now(timezone.utc), key_source=KeySource.server,
                       options=RunOptions(), video=VideoInfo(width=400, height=200, fps=10, frame_count=25, duration_sec=2.5)))
    r = client.post("/api/dev/people", data={"run_id": "r1", "max_seconds": 1})
    assert r.status_code == 201
    body = _wait(client, r.json()["id"])
    assert body["status"] == "completed", body["error"]
    assert body["source"] == "Gate cam" and body["report"]["frames"] == 10
    assert client.post("/api/dev/people", data={"run_id": "missing"}).status_code == 404


def test_rejects_bad_requests(client, tmp_path):
    assert _start(client, tmp_path, '{"detector_imgsz": 650}').status_code == 422
    assert _start(client, tmp_path, '{"question_template": "{item}"}').status_code == 422
    assert _start(client, tmp_path, '{"person_model": "nope.pt"}').status_code == 422
    assert _start(client, tmp_path, "not json").status_code == 422
    assert client.post("/api/dev/people", data={}).status_code == 422
    bad = client.post("/api/dev/people", files={"video": ("x.txt", b"hi", "text/plain")})
    assert bad.status_code == 415
    broken = client.post("/api/dev/people", files={"video": ("x.mp4", b"not a video", "video/mp4")})
    assert broken.status_code == 400
    assert client.get("/api/dev/people").json() == []


def test_delete_a_test(client, tmp_path):
    test_id = _start(client, tmp_path).json()["id"]
    _wait(client, test_id)
    assert client.delete(f"/api/dev/people/{test_id}").status_code == 204
    assert client.get(f"/api/dev/people/{test_id}").status_code == 404
    assert client.delete(f"/api/dev/people/{test_id}").status_code == 404
    assert not (tmp_path / "runs" / "_people_playground" / test_id).exists()
