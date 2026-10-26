"""The inference flowchart app records every Moondream call and decision of a run."""

import time

import pytest
from fastapi.testclient import TestClient

from app.core.settings import Settings
from inference_trace.main import create_app
from inference_trace.recorder import TracingAnalyzer

from .test_api import _video
from .test_pipeline import FakeModel, FakePeople


@pytest.fixture
def client(tmp_path):
    settings = Settings(data_dir=tmp_path / "runs", server_api_key="server-key")
    app = create_app(settings, folder=tmp_path / "traces",
                     analyzer_factory=lambda config, rec: TracingAnalyzer(config, rec, model=FakeModel()),
                     people_factory=lambda fps: FakePeople())
    with TestClient(app) as c:
        yield c


def _run(client, tmp_path, **form):
    src = _video(tmp_path / "site.mp4")
    with src.open("rb") as f:
        r = client.post("/api/traces", files={"video": ("site.mp4", f, "video/mp4")},
                        data={"required_ppe": "helmet", "settings_choice": "defaults", **form})
    assert r.status_code == 201, r.text
    trace_id = r.json()["id"]
    deadline = time.time() + 20
    while time.time() < deadline:
        t = client.get(f"/api/traces/{trace_id}").json()
        if t["status"] in ("completed", "failed"):
            return trace_id, t
        time.sleep(0.1)
    raise AssertionError("trace did not finish")


def test_trace_records_every_call_and_decision(client, tmp_path):
    trace_id, t = _run(client, tmp_path)
    assert t["status"] == "completed", t["error"]
    assert t["settings"]["questions"]["helmet"].endswith("Answer only yes or no.")
    assert [c["frame"] for c in t["checks"]] == [0, 10, 20]
    assert {tr["id"] for tr in t["tracks"]} == {1, 2}

    # The right-hand worker (#2): asked about the helmet, said no, detect found no hard hat
    # but did find a head, so the helmet is missing.
    w = next(w for w in t["checks"][1]["workers"] if w["track_id"] == 2)
    item = w["trace"]["items"]["helmet"]
    assert item["status"] == "missing"
    calls = [s for s in item["steps"] if s["step"] == "call"]
    assert [c["kind"] for c in calls] == ["query", "detect", "detect"]
    assert calls[0]["prompt"] == t["settings"]["questions"]["helmet"] and calls[0]["response"]["answer"] == "No."
    assert [c["prompt"] for c in calls[1:]] == ["hard hat", "head"]
    assert next(s for s in item["steps"] if s["step"] == "query")["read_as"] == "no"
    assert w["violations"] == ["No Helmet"] and w["window"] and w["drawn"]["tag"]

    # Every image the flowchart shows was saved.
    for name in [t["checks"][1]["image"], t["checks"][1]["annotated"], w["trace"]["crop"], calls[0]["image"]]:
        assert client.get(f"/api/traces/{trace_id}/files/images/{name}").status_code == 200
    assert client.get(f"/api/traces/{trace_id}/files/annotated.mp4").status_code == 200
    assert client.get(f"/api/traces/{trace_id}/files/input.mp4").status_code == 404  # the upload isn't served
    assert t["summary"]["events"] and t["calls"]["query"] >= 3
    assert client.get("/api/traces").json()[0]["id"] == trace_id


def test_trace_needs_a_known_item(client, tmp_path):
    src = _video(tmp_path / "site.mp4")
    with src.open("rb") as f:
        r = client.post("/api/traces", files={"video": ("site.mp4", f, "video/mp4")}, data={"required_ppe": "cape"})
    assert r.status_code == 422
