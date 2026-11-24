"""Live camera wall: looping simulated cameras with a fake Moondream and a fake tracker."""

import time

import pytest
from fastapi.testclient import TestClient

from app.core.settings import Settings
from app.main import create_app
from app.services.cameras import camera_id, camera_name
from ppe.analyzer import PPEAnalyzer

from .test_api import _video
from .test_pipeline import FakeModel, FakePeople


@pytest.fixture
def client(tmp_path):
    cams = tmp_path / "cams"
    cams.mkdir()
    _video(cams / "left_corner_2-w1.mp4", frames=40)
    _video(cams / "parking.mp4", frames=40)
    settings = Settings(data_dir=tmp_path / "runs", server_api_key="server-key", camera_dir=cams)
    app = create_app(settings, analyzer_factory=lambda cfg: PPEAnalyzer(cfg, model=FakeModel()),
                     key_verifier=lambda key, endpoint: None, people_factory=lambda fps: FakePeople())
    with TestClient(app) as c:
        yield c


def _wait(fn, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError("timed out")


def test_names_drop_the_site_suffix():
    assert camera_id(__import__("pathlib").Path("left_corner_2-w1.mp4")) == "left_corner_2"
    assert camera_name("left_corner_2") == "Left Corner 2"


def test_catalog_lists_every_video_and_none_are_running(client):
    body = client.get("/api/cameras").json()
    assert [c["id"] for c in body["cameras"]] == ["left_corner_2", "parking"]
    assert not any(c["active"] for c in body["cameras"])
    assert body["totals"]["live"] == 0


def test_start_loops_the_video_and_reports_violations_then_remove(client):
    r = client.post("/api/cameras/start", json={"camera_ids": ["left_corner_2"], "analysis_interval_sec": 0.5})
    assert r.status_code == 200 and r.json() == {"started": ["left_corner_2"]}

    def state():
        return client.get("/api/cameras").json()

    # The 4 s video plays again and again: a second loop proves the stream doesn't end.
    _wait(lambda: next(c for c in state()["cameras"] if c["id"] == "left_corner_2").get("loop", 0) >= 2)
    body = state()
    cam = next(c for c in body["cameras"] if c["id"] == "left_corner_2")
    assert cam["active"] and cam["state"] in ("live", "starting")
    assert not next(c for c in body["cameras"] if c["id"] == "parking")["active"]

    events = _wait(lambda: state()["violations"])
    e = events[0]
    assert e["camera_id"] == "left_corner_2" and e["camera_name"] == "Left Corner 2"
    assert e["violations"] == ["No Helmet", "No Vest"]
    assert client.get(e["crop_url"]).headers["content-type"] == "image/jpeg"
    assert client.get(e["frame_url"]).status_code == 200
    assert client.get(e["clean_url"]).status_code == 200

    with client.websocket_connect("/api/cameras/left_corner_2/live?size=thumb") as ws:
        assert ws.receive_bytes()[:2] == b"\xff\xd8"

    assert client.delete("/api/cameras/left_corner_2").status_code == 204
    after = state()
    assert not next(c for c in after["cameras"] if c["id"] == "left_corner_2")["active"]
    assert after["violations"], "a removed camera's violations stay in the list"
    assert client.delete("/api/cameras/left_corner_2").status_code == 404
    assert client.get(e["crop_url"]).status_code == 200

    assert client.delete("/api/cameras/violations").status_code == 204
    assert state()["violations"] == []


def test_start_rejects_unknown_camera_and_bad_key(client):
    assert client.post("/api/cameras/start", json={"camera_ids": ["nope"]}).status_code == 404
    assert client.get("/api/cameras/x/images/p1/snapshots/..%2Fa.jpg").status_code == 404


def test_frame_reader_can_sample_every_nth_frame(tmp_path):
    from ppe.media import FrameReader

    src = _video(tmp_path / "v.mp4", frames=40, fps=10)
    r = FrameReader(src, every=2)
    n = 0
    while r.read() is not None:
        n += 1
    r.close()
    assert r.fps == 5 and r.frame_count == 20
    assert abs(n - 20) <= 3  # timestamp rounding at the end


def test_pdf_report_lists_cameras_and_violations(client):
    # With nothing started the report still builds.
    r = client.get("/api/cameras/report")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF") and "attachment" in r.headers["content-disposition"]

    client.post("/api/cameras/start", json={"camera_ids": ["left_corner_2", "parking"], "analysis_interval_sec": 0.5})
    _wait(lambda: client.get("/api/cameras").json()["violations"])
    r = client.get("/api/cameras/report")
    assert r.status_code == 200 and r.content.startswith(b"%PDF") and len(r.content) > 3000
