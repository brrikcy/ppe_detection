"""Developer settings: validation, effect on the pipeline, and the /api/dev endpoints."""

import io
import json
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from app.core.settings import Settings
from app.main import create_app
from ppe.analyzer import PPEAnalyzer, WorkerResult
from ppe.config import Config
from ppe.status import PersonStatus
from ppe.tuning import Tuning

from .test_pipeline import FakeModel, FakePeople, write_video

BRIGHT = Image.fromarray(np.full((200, 120, 3), 200, np.uint8))  # FakeModel says "no" to bright crops


# ---- validation ---------------------------------------------------------------
@pytest.mark.parametrize("bad", [
    {"question_template": "Is this person wearing a helmet?"},  # no {item}
    {"items": {"helmet": {"question": "x", "detect_term": "y", "region_top": 0.5, "region_bottom": 0.2}}},
    {"items": {"cape": {"question": "x", "detect_term": "y"}}},
    {"items": {"helmet": {"question": "  ", "detect_term": "y"}}},
    {"detector_imgsz": 650},
    {"person_model": "../secrets.pt"},
    {"person_model": "yolo.txt"},
    {"vote_window": 0},
    {"crop_padding": 2},
])
def test_invalid_settings_are_rejected(bad):
    with pytest.raises(ValidationError):
        Tuning(**bad)


def test_missing_items_fall_back_to_defaults_and_changes_are_listed():
    t = Tuning(items={"helmet": {"question": "a helmet", "detect_term": "helmet", "body_part": ""}},
               crop_padding=0.2)
    assert t.items["vest"] == Tuning().items["vest"]
    assert t.items["helmet"].body_part is None
    # Fields not given keep the item's defaults (e.g. its region and size limits).
    assert set(t.changes()) == {"crop_padding", "items.helmet.question", "items.helmet.detect_term",
                                "items.helmet.body_part"}
    assert Tuning().changes() == []


# ---- effect on the analyzer ------------------------------------------------------
class Recording(FakeModel):
    def __init__(self, answer=None):
        self.questions, self.answer = [], answer

    def query(self, image, question):
        self.questions.append(question)
        return {"answer": self.answer} if self.answer else super().query(image, question)


def analyze(tuning, model, item="helmet"):
    a = PPEAnalyzer(Config(api_key="x", required_ppe=[item], tuning=tuning), model=model)
    try:
        return a.explain_item(BRIGHT, item)
    finally:
        a.close()


def test_custom_prompt_is_sent():
    m = Recording()
    t = Tuning(question_template="Q: {item}?", items={"helmet": {"question": "a red hat", "detect_term": "red hat"}})
    out = analyze(t, m)
    assert m.questions == ["Q: a red hat?"]
    assert out["steps"][0]["question"] == "Q: a red hat?"


@pytest.mark.parametrize("rule", ["present", "unverified", "missing"])
def test_unclear_answer_rule(rule):
    assert analyze(Tuning(unclear_answer=rule), Recording(answer="maybe"))["status"] == rule


def test_confirmation_steps_can_be_switched_off():
    # Default: "no" -> detect hard hat (none) -> detect head (found) -> missing
    steps = analyze(Tuning(), Recording())["steps"]
    assert [s["step"] for s in steps if s["step"] != "detect_object"] == ["query", "detect", "detect"]
    # No confirmation: the "no" alone decides.
    out = analyze(Tuning(confirm_no_with_detect=False), Recording())
    assert out["status"] == "missing" and [s["step"] for s in out["steps"]] == ["query"]


def test_body_part_check_can_be_switched_off():
    class NoHead(Recording):
        def detect(self, image, obj):
            return {"objects": []}
    assert analyze(Tuning(), NoHead())["status"] == "unverified"
    assert analyze(Tuning(check_body_part=False), NoHead())["status"] == "missing"


def test_min_check_height_and_padding():
    a = PPEAnalyzer(Config(api_key="x", tuning=Tuning(min_check_height_frac=0.5, crop_padding=0)), model=FakeModel())
    [far] = a.check_workers(Image.fromarray(np.zeros((200, 400, 3), np.uint8)), [(10, 10, 50, 90)])
    a.close()
    assert far.too_small


def test_vote_window_and_confirm_checks():
    s = PersonStatus(window=1, confirm=1)
    s.add(WorkerResult((0, 0, 1, 1), ["No Vest"]), 0)
    assert s.display(1, (0, 0, 1, 1), False).violations == ["No Vest"]  # no smoothing with window 1

    s = PersonStatus(window=3, confirm=2)
    s.add(WorkerResult((0, 0, 1, 1), ["No Vest"]), 0)
    assert s.display(1, (0, 0, 1, 1), False).pending  # one check: no verdict yet
    s.add(WorkerResult((0, 0, 1, 1), ["No Vest"]), 1)
    assert s.display(1, (0, 0, 1, 1), False).violations == ["No Vest"]
    with pytest.raises(ValidationError):
        Tuning(vote_window=2, confirm_checks=3)


# ---- API ---------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path):
    configs = []

    def factory(cfg):
        configs.append(cfg)
        return PPEAnalyzer(cfg, model=FakeModel())

    app = create_app(Settings(data_dir=tmp_path / "runs", server_api_key="srv"), analyzer_factory=factory,
                     key_verifier=lambda k: None, people_factory=lambda fps: FakePeople())
    with TestClient(app) as c:
        c.configs, c.tmp = configs, tmp_path
        yield c


def test_get_put_reset(client):
    s = client.get("/api/dev/tuning").json()
    assert s["changes"] == [] and s["active"] == s["defaults"]
    new = {**s["active"], "crop_padding": 0.2, "unclear_answer": "unverified"}
    r = client.put("/api/dev/tuning", json=new)
    assert r.status_code == 200 and set(r.json()["changes"]) == {"crop_padding", "unclear_answer"}
    assert client.get("/api/dev/tuning").json()["active"]["crop_padding"] == 0.2
    assert client.delete("/api/dev/tuning").json()["changes"] == []


def test_invalid_put_is_refused_and_nothing_saved(client):
    bad = {**client.get("/api/dev/tuning").json()["active"], "question_template": "no placeholder"}
    assert client.put("/api/dev/tuning", json=bad).status_code == 422
    missing_model = {**bad, "question_template": "{item}?", "person_model": "nope.pt"}
    assert client.put("/api/dev/tuning", json=missing_model).status_code == 422
    assert client.get("/api/dev/tuning").json()["changes"] == []


def test_runs_keep_the_settings_they_were_started_with(client):
    s = client.get("/api/dev/tuning").json()["active"]
    client.put("/api/dev/tuning", json={**s, "crop_padding": 0.0, "question_template": "Wearing {item}?"})
    src = write_video(client.tmp / "v.mp4")
    with src.open("rb") as f:
        rid = client.post("/api/runs", files={"video": ("v.mp4", f, "video/mp4")}).json()["meta"]["id"]
    client.delete("/api/dev/tuning")  # later changes don't affect this run
    for _ in range(200):
        d = client.get(f"/api/runs/{rid}").json()
        if d["meta"]["status"] == "completed":
            break
        time.sleep(0.05)
    assert d["meta"]["options"]["tuning"]["question_template"] == "Wearing {item}?"
    assert client.configs[-1].tuning.question_template == "Wearing {item}?"
    report = client.get(f"/api/runs/{rid}/report").json()
    assert report["tuning"]["crop_padding"] == 0.0
    # and the playground can use an image from that run
    photo = d["summary"]["people"][0]["photo"]
    r = client.post("/api/dev/try", data={"item": "helmet", "run_id": rid, "folder": "people", "name": photo})
    assert r.status_code == 200, r.text
    assert r.json()["steps"][0]["question"].startswith("Focus on")  # saved settings were reset
    assert r.json()["mode"] == "image"  # a person photo has no frame to go back to
    # A violation snapshot is re-checked on the original frame, with the worker's box and neighbour.
    snap = d["summary"]["events"][0]["snapshot"]
    r = client.post("/api/dev/try", data={"item": "helmet", "run_id": rid, "folder": "snapshots", "name": snap})
    body = r.json()
    assert r.status_code == 200, body
    assert body["mode"] == "run" and "1 other worker" in body["note"]
    assert body["status"] == "missing" and body["image"].startswith("data:image/jpeg;base64,")
    assert body["image_size"] == [132, 166]  # the 120x150 right-hand worker with 5% padding each side


def test_try_with_upload_and_unsaved_settings(client):
    buf = io.BytesIO()
    BRIGHT.save(buf, "JPEG")
    t = {**Tuning().model_dump(), "question_template": "Test {item}"}
    r = client.post("/api/dev/try", data={"item": "vest", "tuning": json.dumps(t)},
                    files={"image": ("w.jpg", buf.getvalue(), "image/jpeg")})
    body = r.json()
    assert r.status_code == 200, body
    assert body["steps"][0]["question"].startswith("Test a high-visibility")
    assert body["status"] == "missing" and body["image_size"] == [120, 200]
    assert client.get("/api/dev/tuning").json()["changes"] == []  # nothing saved


@pytest.mark.parametrize("data,files,code", [
    ({"item": "cape"}, None, 422),
    ({"item": "helmet"}, None, 422),  # no image
    ({"item": "helmet", "run_id": "nope", "folder": "people", "name": "1.jpg"}, None, 404),
    ({"item": "helmet", "run_id": "x", "folder": "../..", "name": "meta.json"}, None, 422),
    ({"item": "helmet"}, {"image": ("a.jpg", b"not an image", "image/jpeg")}, 400),
    ({"item": "helmet", "tuning": "{\"vote_window\": 0}"}, None, 422),
])
def test_try_rejects_bad_requests(client, data, files, code):
    assert client.post("/api/dev/try", data=data, files=files).status_code == code
