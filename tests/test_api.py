"""API tests with a fake Moondream model (no network, no API key)."""

import time

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.core.settings import Settings
from app.main import create_app
from app.services.keys import KeyRejected
from ppe.analyzer import PPEAnalyzer

from .test_pipeline import FakeModel, FakePeople


def _video(path, frames=25, fps=10):
    w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (400, 200))
    for _ in range(frames):
        f = np.zeros((200, 400, 3), np.uint8)
        f[:, 200:] = 200
        w.write(f)
    w.release()
    return path


@pytest.fixture
def client(tmp_path):
    settings = Settings(data_dir=tmp_path / "runs", server_api_key="server-key")
    seen_keys = []

    def factory(config):
        seen_keys.append(config.api_key)
        return PPEAnalyzer(config, model=FakeModel())

    def verify(key):
        if key == "bad-key":
            raise KeyRejected("Moondream rejected this API key.")

    app = create_app(settings, analyzer_factory=factory, key_verifier=verify,
                     people_factory=lambda fps: FakePeople())
    with TestClient(app) as c:
        c.seen_keys = seen_keys
        yield c


def _wait(client, run_id, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/runs/{run_id}").json()
        if body["meta"]["status"] in ("completed", "failed"):
            return body
        time.sleep(0.1)
    raise AssertionError("run did not finish")


def _upload(client, tmp_path, **form):
    src = _video(tmp_path / "site.mp4")
    with src.open("rb") as f:
        return client.post("/api/runs", files={"video": ("site.mp4", f, "video/mp4")}, data=form)


def test_config_reports_server_key(client):
    body = client.get("/api/config").json()
    assert body["server_key_configured"] is True
    assert {"key": "helmet", "label": "Helmet"} in body["ppe_items"]


def test_full_run_lifecycle(client, tmp_path):
    r = _upload(client, tmp_path, required_ppe="helmet,vest", analysis_interval_sec="1")
    assert r.status_code == 201, r.text
    run_id = r.json()["meta"]["id"]

    body = _wait(client, run_id)
    assert body["meta"]["status"] == "completed"
    assert body["meta"]["key_source"] == "server"
    assert client.seen_keys == ["server-key"]
    s = body["summary"]
    assert s["frames_analysed"] == 3
    assert s["peak_workers"] == 2
    assert s["violation_checks"] == 3
    assert {t["label"]: t["count"] for t in s["by_type"]} == {"No Helmet": 3, "No Vest": 3}
    snap = s["events"][0]["snapshot"]
    assert snap and client.get(f"/api/runs/{run_id}/snapshots/{snap}").status_code == 200
    full = s["events"][0]["frame_image"]
    assert full and client.get(f"/api/runs/{run_id}/frames/{full}").status_code == 200

    # Person tracking: two people, the right-hand one always missing helmet and vest.
    assert s["tracked"] is True
    people = {p["id"]: p for p in s["people"]}
    assert set(people) == {1, 2}
    assert people[1]["status_label"] == "PPE OK" and people[1]["checks"] == 3
    assert people[2]["status_label"] == "PPE Violation (2) - No Helmet, No Vest"
    assert s["people"][0]["id"] == 2  # violating people listed first
    assert {e["track_id"] for e in s["events"]} == {2}
    photo = people[2]["photo"]
    assert photo and client.get(f"/api/runs/{run_id}/people/{photo}").status_code == 200

    assert client.get(f"/api/runs/{run_id}/video").headers["content-type"] == "video/mp4"
    assert client.get(f"/api/runs/{run_id}/report").json()["frames_analysed"] == 3
    assert client.get(f"/api/runs/{run_id}/preview").status_code == 200

    listing = client.get("/api/runs").json()
    assert listing[0]["id"] == run_id and listing[0]["violation_checks"] == 3

    assert client.delete(f"/api/runs/{run_id}").status_code == 204
    assert client.get(f"/api/runs/{run_id}").status_code == 404


def test_form_key_takes_precedence_and_is_not_stored(client, tmp_path):
    r = _upload(client, tmp_path, api_key="form-key")
    run_id = r.json()["meta"]["id"]
    _wait(client, run_id)
    assert client.seen_keys == ["form-key"]
    meta_file = tmp_path / "runs" / run_id / "meta.json"
    assert "form-key" not in meta_file.read_text()


def test_wrong_key_is_refused_before_a_run_is_created(client, tmp_path):
    r = _upload(client, tmp_path, api_key="bad-key")
    assert r.status_code == 401
    assert "rejected" in r.json()["detail"]
    assert client.get("/api/runs").json() == []
    assert client.seen_keys == []  # nothing was processed

    check = client.post("/api/key/check", data={"api_key": "bad-key"}).json()
    assert check == {"ok": False, "source": "form", "message": "Moondream rejected this API key."}
    assert client.post("/api/key/check").json()["ok"] is True  # server key


def test_rejects_bad_input(client, tmp_path):
    bad = tmp_path / "notes.txt"
    bad.write_text("hello")
    with bad.open("rb") as f:
        r = client.post("/api/runs", files={"video": ("notes.txt", f, "text/plain")})
    assert r.status_code == 415

    fake = tmp_path / "broken.mp4"
    fake.write_bytes(b"not a video")
    with fake.open("rb") as f:
        r = client.post("/api/runs", files={"video": ("broken.mp4", f, "video/mp4")})
    assert r.status_code == 400

    assert _upload(client, tmp_path, required_ppe="helmet,cape").status_code == 422


def test_missing_key_everywhere(tmp_path):
    app = create_app(Settings(data_dir=tmp_path / "runs", server_api_key=None))
    with TestClient(app) as c:
        assert c.get("/api/config").json()["server_key_configured"] is False
        assert _upload(c, tmp_path).status_code == 422
        assert c.post("/api/key/check").json()["ok"] is False


def test_path_traversal_is_rejected(client):
    assert client.get("/api/runs/..%2F..%2Fetc").status_code == 404


def test_mp4v_video_is_converted_for_browsers(tmp_path):
    from app.services.media import VideoConverter
    from ppe.media import find_ffmpeg, is_browser_playable, video_fourcc

    if not find_ffmpeg():
        pytest.skip("no ffmpeg available")
    path = _video(tmp_path / "annotated.mp4")
    assert not is_browser_playable(path)  # OpenCV reports mp4v as FMP4
    assert VideoConverter().ensure_playable(path)
    assert video_fourcc(path) in {"avc1", "h264"}
