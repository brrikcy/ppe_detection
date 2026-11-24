"""Settings state 1 (the initial prompt and its double-checks) next to state 2 (the current prompt).

The state 1 analyzer tests are the ones that covered the method before the visibility prompt replaced
it; they run on Tuning.for_state("1").
"""

import json

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.core.settings import Settings
from app.main import create_app
from app.services.dev_settings import DevSettingsStore
from ppe.analyzer import PPEAnalyzer
from ppe.config import Config
from ppe.tuning import Tuning

from .test_pipeline import FakeModel, FakePeople

BOX = (100, 100, 300, 500)  # a worker in an 800x600 frame
IMG = Image.fromarray(np.zeros((600, 800, 3), np.uint8))
BRIGHT = Image.fromarray(np.full((200, 120, 3), 200, np.uint8))  # FakeModel says "no" to bright crops


def one(**changes) -> Tuning:
    """State 1's defaults with some settings changed."""
    return Tuning.for_state("1").model_copy(update=changes)


def norm(box, crop=BOX):
    cx1, cy1, cx2, cy2 = crop
    w, h = cx2 - cx1, cy2 - cy1
    return {"x_min": (box[0] - cx1) / w, "y_min": (box[1] - cy1) / h,
            "x_max": (box[2] - cx1) / w, "y_max": (box[3] - cy1) / h}


class Scripted:
    """query: "no" to PPE questions, `verify` to "Is this a ..."; detect returns `found[term]` boxes."""

    def __init__(self, found=None, answer="no", verify="yes"):
        self.found, self.answer, self.verify, self.questions = found or {}, answer, verify, []

    def query(self, image, question):
        self.questions.append(question)
        return {"answer": self.verify if question.startswith("Is this a") else self.answer}

    def detect(self, image, obj):
        return {"objects": [norm(b) for b in self.found.get(obj, [])]}


def status(model, item, tuning=None, **changes):
    tuning = tuning or one(crop_padding=0.0, **changes)
    a = PPEAnalyzer(Config(api_key="x", required_ppe=[item], tuning=tuning), model=model)
    out = a.explain_item(IMG, item, BOX, ())
    a.close()
    return out


# ---- the two states ----------------------------------------------------------------
def test_state_2_is_the_default_and_state_1_has_the_initial_method():
    two, initial = Tuning(), Tuning.for_state("1")
    assert two.settings_state == "2" and initial.settings_state == "1"
    assert "visible" in two.question_for("helmet") and "visible" not in initial.question_for("helmet")
    assert initial.question_for("helmet").endswith("Answer only yes or no.")
    assert (initial.confirm_no_with_detect, initial.verify_detected_box, initial.check_body_part) == (True,) * 3
    assert (two.confirm_no_with_detect, two.verify_detected_box, two.check_body_part) == (False,) * 3
    assert initial.items["helmet"].body_part == "head" and two.items["helmet"].body_part is None
    assert initial.items["boots"].body_part_question and not two.items["boots"].body_part_question
    assert initial.changes() == [] and two.changes() == []  # each is compared with its own defaults
    assert initial.model_copy(update={"crop_padding": 0.2}).changes() == ["crop_padding"]
    with pytest.raises(ValueError):
        Tuning.for_state("3")


def test_the_combined_question_names_the_body_part_only_in_state_2():
    assert "(body part: the head)" in Tuning().multi_question_for(["helmet"])
    assert "body part" not in Tuning.for_state("1").multi_question_for(["helmet"])


# ---- state 1 in the analyzer ---------------------------------------------------------
def test_helmet_detected_as_vest_does_not_overrule_no():
    helmet = (170, 95, 230, 150)
    out = status(Scripted({"high visibility vest": [helmet]}), "vest")
    assert out["status"] == "missing"
    [obj] = [s for s in out["steps"] if s["step"] == "detect_object"]
    assert obj["reason"] in ("wrong size", "outside region")


def test_cap_detected_as_hard_hat_is_rechecked():
    cap = (170, 95, 230, 140)
    out = status(Scripted({"hard hat": [cap], "head": [cap]}, verify="No, it's a baseball cap."), "helmet")
    assert out["status"] == "missing"
    assert [s["reason"] for s in out["steps"] if s["step"] == "detect_object"][0] == "re-check said no"
    assert any(s["step"] == "verify" and not s["confirmed"] for s in out["steps"])
    assert status(Scripted({"hard hat": [cap]}, verify="Yes"), "helmet")["status"] == "present"
    assert status(Scripted({"hard hat": [cap]}, verify="No"), "helmet", verify_detected_box=False)["status"] == "present"


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


def test_state_1_confirmation_steps_can_be_switched_off():
    # "no" -> detect hard hat (none) -> detect head (found) -> missing
    steps = analyze(one(), Recording())["steps"]
    assert [s["step"] for s in steps if s["step"] != "detect_object"] == ["query", "detect", "detect"]
    out = analyze(one(confirm_no_with_detect=False), Recording())
    assert out["status"] == "missing" and [s["step"] for s in out["steps"] if s["step"] != "detect_object"] == ["query", "detect"]
    out = analyze(one(confirm_no_with_detect=False, check_body_part=False), Recording())
    assert out["status"] == "missing" and [s["step"] for s in out["steps"]] == ["query"]


class NoHead(Recording):
    def detect(self, image, obj):
        return {"objects": []}


def test_state_1_body_part_check():
    assert analyze(one(), NoHead())["status"] == "unverified"
    assert analyze(one(check_body_part=False), NoHead())["status"] == "missing"
    # State 2 asks the prompt instead: a plain "no" is a violation, with no detect calls.
    assert analyze(Tuning(), NoHead())["status"] == "missing"


class FeetAnswer(Recording):
    def __init__(self, feet):
        super().__init__()
        self.feet = feet

    def query(self, image, question):
        self.questions.append(question)
        return {"answer": self.feet if "feet" in question else "No."}

    def detect(self, image, obj):
        return {"objects": [{"x_min": 0.4, "y_min": 0.8, "x_max": 0.5, "y_max": 0.9}]}


@pytest.mark.parametrize("feet, expected", [("No", "unverified"), ("Not visible", "unverified"), ("Yes", "missing")])
def test_state_1_boots_ask_whether_the_feet_are_visible(feet, expected):
    out = analyze(one(confirm_no_with_detect=False), FeetAnswer(feet), item="boots")
    assert out["status"] == expected
    assert [s["step"] for s in out["steps"]] == ["query", "body_part"]


class OccludedModel:
    """Query says "no" for everything, but detect finds a vest and no head."""

    def detect(self, image, obj):
        box = {"x_min": 0.1, "y_min": 0.3, "x_max": 0.9, "y_max": 0.6}
        return {"objects": [box] if obj == "high visibility vest" else []}

    def query(self, image, question):
        return {"answer": "yes" if question.startswith("Is this a") else "no"}


def test_state_1_no_answer_is_confirmed_with_detect():
    a = PPEAnalyzer(Config(api_key="x", tuning=Tuning.for_state("1")), model=OccludedModel())
    [r] = a.check_workers(Image.fromarray(np.zeros((200, 400, 3), np.uint8)), [(40, 0, 180, 180)])
    a.close()
    assert r.label == "PPE OK" and r.unverified == ["helmet"]  # vest found by detect; no head to wear a helmet on


# ---- saving, switching, resetting ----------------------------------------------------
def test_each_state_keeps_its_own_saved_settings(tmp_path):
    store = DevSettingsStore(tmp_path)
    assert store.active_state() == "2" and store.get().settings_state == "2"
    store.save(Tuning.for_state("1").model_copy(update={"crop_padding": 0.2}))
    assert store.active_state() == "1" and store.get().crop_padding == 0.2
    store.set_active("2")
    assert store.get().crop_padding == Tuning().crop_padding and store.get("1").crop_padding == 0.2
    store.save(Tuning(vote_window=5))
    assert store.get("2").vote_window == 5 and store.get("1").vote_window == 3
    store.set_active("1")
    assert store.reset().settings_state == "1" and store.get("1").crop_padding == Tuning().crop_padding
    assert store.get("2").vote_window == 5  # the other state's settings stay
    with pytest.raises(ValueError):
        store.set_active("3")


def test_settings_saved_before_there_were_two_states_become_state_2(tmp_path):
    path = tmp_path / "_dev" / "tuning.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(Tuning(crop_padding=0.3).model_dump()))
    store = DevSettingsStore(tmp_path)
    assert store.active_state() == "2" and store.get().crop_padding == 0.3
    assert store.get("1").crop_padding == Tuning.for_state("1").crop_padding


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(data_dir=tmp_path / "runs", server_api_key="k"),
                     analyzer_factory=lambda cfg: PPEAnalyzer(cfg, model=FakeModel()),
                     key_verifier=lambda key, endpoint: None, people_factory=lambda fps: FakePeople())
    with TestClient(app) as c:
        yield c


def test_api_switches_between_the_two_states(client):
    body = client.get("/api/dev/tuning").json()
    assert body["state"] == "2" and body["active"]["settings_state"] == "2" and body["changes"] == []
    body = client.put("/api/dev/state", json={"state": "1"}).json()
    assert body["state"] == "1" and body["defaults"]["settings_state"] == "1"
    assert body["active"]["confirm_no_with_detect"] is True and "visible" not in body["active"]["question_template"]
    body["active"]["crop_padding"] = 0.2
    saved = client.put("/api/dev/tuning", json=body["active"]).json()
    assert saved["state"] == "1" and saved["changes"] == ["crop_padding"]
    body = client.put("/api/dev/state", json={"state": "2"}).json()
    assert body["state"] == "2" and body["changes"] == [] and body["active"]["crop_padding"] == Tuning().crop_padding
    assert client.get("/api/dev/tuning").json()["state"] == "2"
    assert client.put("/api/dev/state", json={"state": "3"}).status_code == 422
    # Reset only affects the active state.
    client.put("/api/dev/state", json={"state": "1"})
    assert client.delete("/api/dev/tuning").json()["changes"] == []


def test_a_run_uses_the_active_state(client, tmp_path):
    from .test_api import _upload, _wait

    client.put("/api/dev/state", json={"state": "1"})
    run = _upload(client, tmp_path).json()
    done = _wait(client, run["meta"]["id"])
    assert done["meta"]["options"]["tuning"]["settings_state"] == "1"
