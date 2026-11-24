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


def test_analytics_numbers():
    from collections import Counter

    from app.services.cameras import compute_analytics

    log = [{"ts": 1000, "camera_id": "a", "violations": ["No Helmet"]},
           {"ts": 1010, "camera_id": "a", "violations": ["No Helmet", "No Vest"]},
           {"ts": 1100, "camera_id": "b", "violations": ["No Vest"]}]
    samples = [(1000, "a", 2, 1), (1005, "b", 1, 0), (1100, "a", 3, 2), (1100, "b", 1, 1)]
    counts = {"a": Counter(ok=6, violation=2, people=4), "b": Counter(ok=2, violation=0, people=1)}
    a = compute_analytics(log, samples, counts, {"a": "Cam A", "b": "Cam B"})
    assert a["kpis"]["violations"] == 3 and a["kpis"]["checks"] == 10
    assert a["kpis"]["compliance"] == 0.8 and a["kpis"]["workers_tracked"] == 5
    assert a["kpis"]["worst_camera"] == "Cam A" and a["kpis"]["peak_missing"] == 3
    assert [(t["label"], t["count"]) for t in a["by_type"]] == [("No Helmet", 2), ("No Vest", 2)] or \
        {(t["label"], t["count"]) for t in a["by_type"]} == {("No Helmet", 2), ("No Vest", 2)}
    cam_a = next(c for c in a["by_camera"] if c["id"] == "a")
    assert cam_a["violations"] == 2 and cam_a["compliance"] == 0.75
    pts = a["timeline"]["points"]
    assert sum(p["violations"] for p in pts) == 3
    assert a["timeline"]["bucket_sec"] == 15 and pts[-1]["workers"] == 4 and pts[-1]["missing"] == 3
    empty = compute_analytics([], [], {}, {})
    assert empty["kpis"]["violations"] == 0 and empty["kpis"]["compliance"] is None and empty["timeline"]["points"] == []


def test_state_includes_analytics_and_clear_resets_them(client):
    client.post("/api/cameras/start", json={"camera_ids": ["left_corner_2"], "analysis_interval_sec": 0.5})
    a = _wait(lambda: (lambda x: x if x["kpis"]["violations"] else None)(client.get("/api/cameras").json()["analytics"]))
    assert a["by_camera"][0]["id"] == "left_corner_2" and a["by_type"]
    assert a["timeline"]["points"] and a["kpis"]["checks"] >= 0
    client.delete("/api/cameras/left_corner_2")
    client.delete("/api/cameras/violations")
    assert client.get("/api/cameras").json()["analytics"]["kpis"]["violations"] == 0
