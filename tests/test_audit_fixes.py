"""Regression tests for the tracking, detection and voting audit (finding IDs in brackets)."""

import numpy as np
import pytest
from PIL import Image

from ppe.analyzer import PPEAnalyzer, WorkerResult, _is_yes, _parse_multi
from ppe.status import PersonStatus
from ppe.tuning import Tuning
from ppe.video import CHECKS_IN_FLIGHT, process_video

from .test_pipeline import LEFT, RIGHT, FakeModel, FakePeople, make_cfg, write_video

BOX = (100, 100, 300, 500)  # a worker in an 800x600 frame
IMG = Image.fromarray(np.zeros((600, 800, 3), np.uint8))


def norm(box, crop=BOX):
    """Frame-pixel box -> detect() output relative to the crop (no padding)."""
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


def status(model, item, others=(), **tuning):
    a = PPEAnalyzer(make_cfg(crop_padding=0.0, required_ppe=[item], **tuning), model=model)
    out = a.explain_item(IMG, item, BOX, others)
    a.close()
    return out


# ---- [D3] reading answers ------------------------------------------------------------
@pytest.mark.parametrize("answer,expected", [
    ("Yes", True), ("yes, partly", True), ("No.", False), ("no", False), ("Nope", False),
    ("Not sure", None), ("Not visible", None), ("None", None), ("Noted, yes", None), ("No idea", None),
    ("I can't tell", None), ("", None),
])
def test_answers_are_read_by_whole_words(answer, expected):
    assert _is_yes(answer) is expected


def test_combined_answer_is_parsed():
    assert _parse_multi("helmet: yes\nVest - no\ngloves: not sure", ["helmet", "vest", "gloves", "boots"]) == \
        {"helmet": "yes", "vest": "no", "gloves": "not"}


# ---- [D1] the detect double-check ------------------------------------------------------
def test_helmet_detected_as_vest_does_not_overrule_no():
    # A head-sized box at the top of the worker: inside the vest band by its centre, but the
    # wrong size and mostly above the vest region.
    helmet = (170, 95, 230, 150)
    out = status(Scripted({"high visibility vest": [helmet]}), "vest")
    assert out["status"] == "missing"
    [obj] = [s for s in out["steps"] if s["step"] == "detect_object"]
    assert obj["reason"] in ("wrong size", "outside region")


def test_cap_detected_as_hard_hat_is_rechecked():
    cap = (170, 95, 230, 140)  # right place and size for a helmet
    out = status(Scripted({"hard hat": [cap], "head": [cap]}, verify="No, it's a baseball cap."), "helmet")
    assert out["status"] == "missing"
    assert [s["reason"] for s in out["steps"] if s["step"] == "detect_object"][0] == "re-check said no"
    assert any(s["step"] == "verify" and not s["confirmed"] for s in out["steps"])
    # A real hard hat that Moondream confirms still overrules the "no".
    assert status(Scripted({"hard hat": [cap]}, verify="Yes"), "helmet")["status"] == "present"
    # Re-check switched off: detect alone decides, as before.
    assert status(Scripted({"hard hat": [cap]}, verify="No"), "helmet", verify_detected_box=False)["status"] == "present"


def test_detected_box_must_be_mostly_inside_the_region():
    # A tall "vest" box centred on the torso but reaching far above and below the vest band.
    tall = (130, 0, 270, 600)
    assert status(Scripted({"high visibility vest": [tall]}), "vest")["status"] == "missing"
    vest = (120, 180, 280, 380)
    assert status(Scripted({"high visibility vest": [vest]}), "vest")["status"] == "present"


# ---- [D2] guarding a "yes" ------------------------------------------------------------------
def test_yes_is_confirmed_when_a_neighbour_overlaps():
    neighbour = (200, 100, 400, 500)  # covers half of the worker
    alone = Scripted(answer="yes")
    assert status(alone, "helmet")["status"] == "present" and len(alone.questions) == 1  # no extra calls
    crowded = status(Scripted(answer="yes"), "helmet", others=[neighbour])
    assert crowded["status"] == "unverified"  # detect found no helmet on this worker
    assert any(s["step"] == "overlap" for s in crowded["steps"])
    own = (150, 95, 210, 140)  # left part of the worker's head: theirs, not the neighbour's
    assert status(Scripted({"hard hat": [own]}, answer="yes"), "helmet", others=[neighbour])["status"] == "present"
    assert status(Scripted(answer="yes"), "helmet", others=[neighbour],
                  confirm_yes_on_overlap=False)["status"] == "present"


# ---- [D4] minimum size in pixels ----------------------------------------------------------
def test_min_height_is_in_pixels_as_well():
    t = Tuning()
    assert t.min_check_height(720) == 100 and t.min_check_height(2160) == pytest.approx(172.8)
    a = PPEAnalyzer(make_cfg(), model=FakeModel())
    [r] = a.check_workers(Image.fromarray(np.zeros((720, 1280, 3), np.uint8)), [(0, 0, 50, 80)])
    a.close()
    assert r.too_small  # 80 px is 11% of 720 px, but too few pixels to judge


# ---- [D5] one question per worker ----------------------------------------------------------
def test_one_query_per_worker():
    class Combined(Scripted):
        def query(self, image, question):
            self.questions.append(question)
            return {"answer": "helmet: yes\nvest: yes"}
    m = Combined()
    a = PPEAnalyzer(make_cfg(crop_padding=0.0, one_query_per_worker=True), model=m)
    [r] = a.check_workers(IMG, [BOX])
    a.close()
    assert r.ok and len(m.questions) == 1

    class Partial(Combined):
        def query(self, image, question):
            self.questions.append(question)
            return {"answer": "helmet: yes" if "vest:" in question else "yes"}
    m = Partial()
    a = PPEAnalyzer(make_cfg(crop_padding=0.0, one_query_per_worker=True), model=m)
    [r] = a.check_workers(IMG, [BOX])
    a.close()
    assert r.ok and len(m.questions) == 2  # vest wasn't in the reply, so it was asked on its own


# ---- [S1] failures ------------------------------------------------------------------------
class RightFails(FakeModel):
    def query(self, image, question):
        if np.asarray(image).mean() > 150:
            raise RuntimeError("HTTP 500")
        return super().query(image, question)


def test_one_failing_worker_does_not_fail_the_run(tmp_path):
    src = write_video(tmp_path / "in.mp4")
    a = PPEAnalyzer(make_cfg(crop_padding=0.0, retry_attempts=1), model=RightFails())
    report = process_video(src, tmp_path / "out.mp4", a, people_factory=lambda fps: FakePeople())
    a.close()
    left = [w for e in report["timeline"] for w in e["workers"] if w["track_id"] == 1]
    right = [w for e in report["timeline"] for w in e["workers"] if w["track_id"] == 2]
    assert [w["label"] for w in left] == ["PPE OK"] * 3
    assert all(w["check_failed"] for w in right) and report["failed_checks"] == 3


def test_partial_failure_keeps_the_other_items():
    class VestFails(FakeModel):
        def query(self, image, question):
            if "vest" in question:
                raise RuntimeError("HTTP 503")
            return {"answer": "no"}
    a = PPEAnalyzer(make_cfg(crop_padding=0.0, retry_attempts=1), model=VestFails())
    [r] = a.check_workers(Image.fromarray(np.full((200, 400, 3), 200, np.uint8)), [RIGHT])
    a.close()
    assert r.violations == ["No Helmet"] and r.failed == ["vest"] and not r.check_failed


def test_run_stops_after_failed_checks_in_a_row(tmp_path):
    class Down(FakeModel):
        calls = 0

        def query(self, image, question):
            Down.calls += 1
            raise RuntimeError("HTTP 503")
    src = write_video(tmp_path / "in.mp4", frames=100)  # 10 regular checks
    a = PPEAnalyzer(make_cfg(required_ppe=["helmet"], retry_attempts=1, max_failed_checks=3), model=Down())
    with pytest.raises(RuntimeError, match="3 checks in a row"):
        process_video(src, tmp_path / "out.mp4", a, people_factory=lambda fps: FakePeople())
    a.close()
    # 3 check frames x 2 workers, no retries in between; checks already sent to Moondream
    # while those failed (tracking doesn't wait) may add up to CHECKS_IN_FLIGHT more frames.
    assert 6 <= Down.calls <= 6 + 2 * CHECKS_IN_FLIGHT


# ---- [S4] [T2] scheduling --------------------------------------------------------------------
def queried_frames(tmp_path, schedule, interval=1.0, frames=25, **tuning):
    """Frames at which worker 2 was checked (asked about), from the report."""
    people = FakePeople(schedule)
    src = write_video(tmp_path / "in.mp4", frames=frames)
    a = PPEAnalyzer(make_cfg(crop_padding=0.0, required_ppe=["helmet"], analysis_interval_sec=interval,
                             parallel_requests=1, **tuning), model=FakeModel())
    report = process_video(src, tmp_path / "out.mp4", a, people_factory=lambda fps: people)
    a.close()
    return [e["frame"] for e in report["timeline"] for w in e["workers"]
            if w["track_id"] == 2 and not w["too_small"] and not w["pending"]]


def test_new_track_waits_before_its_first_check(tmp_path):
    late = lambda i: [(1, LEFT)] + ([(2, RIGHT)] if i >= 2 else [])
    # 10 fps: worker 2 appears at frame 2, is 1 s old at frame 12 -> arrival check there, then 20.
    assert queried_frames(tmp_path, late) == [12, 20]
    # A 0.5 s ghost is never checked.
    ghost = lambda i: [(1, LEFT)] + ([(2, RIGHT)] if 2 <= i <= 6 else [])
    assert queried_frames(tmp_path, ghost) == []


def test_passer_by_in_second_half_of_long_interval_is_checked(tmp_path):
    # 5 s interval at 10 fps: worker 2 is on screen from 3.0 s to 4.0 s only.
    brief = lambda i: [(1, LEFT)] + ([(2, RIGHT)] if 30 <= i <= 40 else [])
    assert queried_frames(tmp_path, brief, interval=5.0, frames=60, min_check_age_sec=0) == [30]


def test_newcomer_at_the_frame_edge_waits(tmp_path):
    entering = lambda i: [(1, LEFT)] + ([((2), (220, 40, 400, 190) if i < 4 else RIGHT)] if i >= 2 else [])
    assert queried_frames(tmp_path, entering, min_check_age_sec=0) == [4, 10, 20]


# ---- [V5] display --------------------------------------------------------------------------
def test_too_far_shows_last_status():
    s = PersonStatus(window=3, confirm=1)
    s.add(WorkerResult((0, 0, 1, 1), ["No Helmet"]), 0)
    far = s.display(4, (0, 0, 1, 1), too_small=True)
    assert far.too_small and far.tag == "#4 Too far (was Missing: Helmet)"
    assert PersonStatus().display(5, (0, 0, 1, 1), too_small=True).tag == "#5 Too far"


# ---- "not visible" as an answer ------------------------------------------------------------
@pytest.mark.parametrize("answer,expected", [
    ("Not visible", "not visible"), ("not visible.", "not visible"), ("Not visible, the head is cut off", "not visible"),
    ("I can't see the head", "not visible"), ("Cannot see", "not visible"), ("Hidden", "not visible"),
    ("Out of view", "not visible"),
    ("yes", "yes"), ("No", "no"), ("No, not visible", "no"),  # yes/no first
    ("Not sure", "unclear"), ("", "unclear"), ("None", "unclear"),
])
def test_not_visible_is_read(answer, expected):
    from ppe.analyzer import _read_answer

    assert _read_answer(answer) == expected


def test_combined_reply_can_say_not_visible():
    got = _parse_multi("helmet: not visible\nvest: yes\ngloves - Not visible.", ["helmet", "vest", "gloves"])
    # The reply is kept as written; the reader that follows ignores case.
    assert {k: v.lower() for k, v in got.items()} == {"helmet": "not visible", "vest": "yes", "gloves": "not visible"}


def test_prompts_offer_not_visible():
    from ppe.tuning import DEFAULT_MULTI_TEMPLATE, DEFAULT_QUESTION_TEMPLATE

    assert "not visible" in DEFAULT_QUESTION_TEMPLATE and "not visible" in DEFAULT_MULTI_TEMPLATE


def test_not_visible_makes_the_item_unverified_with_one_call():
    m = Scripted(answer="Not visible")
    out = status(m, "helmet")
    assert out["status"] == "unverified"
    assert [s["step"] for s in out["steps"]] == ["query"] and out["steps"][0]["read_as"] == "not visible"
    assert len(m.questions) == 1  # no detect, verify or body-part calls


@pytest.mark.parametrize("rule", ["present", "unverified", "missing"])
def test_not_visible_rule(rule):
    assert status(Scripted(answer="not visible"), "vest", not_visible_answer=rule)["status"] == rule


def test_not_visible_and_unclear_have_their_own_rules():
    both = dict(not_visible_answer="missing", unclear_answer="present")
    assert status(Scripted(answer="not visible"), "vest", **both)["status"] == "missing"
    assert status(Scripted(answer="maybe"), "vest", **both)["status"] == "present"


def test_not_visible_in_a_combined_reply():
    class Combined(Scripted):
        def query(self, image, question):
            self.questions.append(question)
            return {"answer": "helmet: not visible\nvest: yes"}
    m = Combined()
    a = PPEAnalyzer(make_cfg(crop_padding=0.0, one_query_per_worker=True), model=m)
    [r] = a.check_workers(IMG, [BOX])
    a.close()
    assert r.unverified == ["helmet"] and r.violations == [] and len(m.questions) == 1


# ---- the part named in the prompt -------------------------------------------------------------
def test_question_names_the_part_for_each_item():
    from ppe.tuning import Tuning

    t = Tuning()
    parts = {"helmet": "the head", "vest": "the upper body", "gloves": "the hands", "boots": "the feet", "goggles": "the face"}
    for item, part in parts.items():
        q = t.question_for(item)
        assert f"Answer not visible if {part} can't be seen" in q, q
        assert "{part}" not in q and "{item}" not in q
    assert "wearing a hard hat" in t.question_for("helmet")


def test_combined_question_names_each_part():
    from ppe.tuning import Tuning

    q = Tuning().multi_question_for(["helmet", "boots"])
    assert "helmet: a hard hat / safety helmet on their head (body part: the head)" in q
    assert "boots: safety boots / work boots (body part: the feet)" in q


def test_part_name_is_editable_and_old_templates_still_work():
    from ppe.tuning import Tuning

    t = Tuning(question_template="Q {item}? Not visible if {part} is hidden.",
               items={"helmet": {"question": "a cap", "detect_term": "cap", "part_name": "the scalp"}})
    assert t.question_for("helmet") == "Q a cap? Not visible if the scalp is hidden."
    # A template without {part} (saved before it existed) is sent as written.
    old = Tuning(question_template="Is this person wearing {item}? Answer yes, no, or not visible.")
    assert old.question_for("vest").endswith("or not visible.") and "upper body" not in old.question_for("vest")
    # Items saved without part_name get the default for their item.
    saved = Tuning.model_validate({"items": {"boots": {"question": "boots", "detect_term": "boot"}}})
    assert saved.items["boots"].part_name == "the feet"


def test_part_name_must_not_be_blank():
    from pydantic import ValidationError

    from ppe.tuning import Tuning

    with pytest.raises(ValidationError):
        Tuning(items={"helmet": {"question": "a cap", "detect_term": "cap", "part_name": "  "}})


def test_the_question_sent_to_moondream_names_the_part():
    m = Scripted(answer="yes")
    status(m, "helmet")
    assert "Answer not visible if the head can't be seen" in m.questions[0]


# ---- two-step replies: "visible: yes or no; wearing: yes, no or n/a" -------------------------------
@pytest.mark.parametrize("answer,expected", [
    # Not visible: wearing is not considered, whatever it says.
    ("visible: no; wearing: no", "not visible"), ("visible: no; wearing: yes", "not visible"),
    ("visible: no; wearing: n/a", "not visible"), ("Visible: No.", "not visible"), ("visible=no", "not visible"),
    ("wearing: yes; visible: no", "not visible"),
    # Visible: only now wearing counts.
    ("visible: yes; wearing: yes", "yes"), ("Visible: Yes. Wearing: No.", "no"),
    ("visible: yes\nwearing: no", "no"), ("visible - yes, wearing - yes", "yes"),
    ("visible: yes; wearing: n/a", "unclear"), ("visible: yes; wearing: maybe", "unclear"),
    ("visible: yes", "unclear"),  # no wearing given
    # Other replies are read as before.
    ("yes", "yes"), ("No", "no"), ("Not visible", "not visible"), ("Not sure", "unclear"),
])
def test_two_step_reply_is_read_visibility_first(answer, expected):
    from ppe.analyzer import _read_answer

    assert _read_answer(answer) == expected


def test_two_step_reply_in_a_combined_answer():
    reply = "helmet: visible: yes; wearing: no\nvest: visible: no; wearing: n/a\ngloves: yes\nboots: not visible"
    got = _parse_multi(reply, ["helmet", "vest", "gloves", "boots"])
    from ppe.analyzer import _read_answer

    assert {k: _read_answer(v) for k, v in got.items()} == \
        {"helmet": "no", "vest": "not visible", "gloves": "yes", "boots": "not visible"}
    # Several items on one line.
    one = _parse_multi("helmet: visible: yes; wearing: yes; vest: visible: no; wearing: no", ["helmet", "vest"])
    assert {k: _read_answer(v) for k, v in one.items()} == {"helmet": "yes", "vest": "not visible"}


def test_visible_no_is_not_visible_whatever_wearing_says():
    for wearing in ("no", "yes", "n/a"):
        m = Scripted(answer=f"visible: no; wearing: {wearing}")
        out = status(m, "helmet")
        assert out["status"] == "unverified", wearing
        assert [s["step"] for s in out["steps"]] == ["query"] and len(m.questions) == 1  # no extra calls
        step = out["steps"][0]
        assert step["read_as"] == "not visible" and step["visible"] == "no" and step["wearing"] == wearing
    # The "not visible" rule applies, as for a plain "not visible".
    assert status(Scripted(answer="visible: no; wearing: no"), "helmet", not_visible_answer="missing")["status"] == "missing"
    assert status(Scripted(answer="visible: no; wearing: yes"), "helmet", not_visible_answer="present")["status"] == "present"


def test_wearing_counts_only_when_visible():
    assert status(Scripted(answer="visible: yes; wearing: yes"), "vest")["status"] == "present"
    assert status(Scripted(answer="visible: yes; wearing: no"), "vest")["status"] == "missing"
    out = status(Scripted(answer="visible: yes; wearing: n/a"), "vest", unclear_answer="unverified")
    assert out["status"] == "unverified" and out["steps"][0]["read_as"] == "unclear"
    assert status(Scripted(answer="visible: yes; wearing: n/a"), "vest", unclear_answer="missing")["status"] == "missing"


def test_two_step_replies_in_one_question_per_worker():
    class Combined(Scripted):
        def query(self, image, question):
            self.questions.append(question)
            return {"answer": "helmet: visible: no; wearing: no\nvest: visible: yes; wearing: no"}
    m = Combined()
    a = PPEAnalyzer(make_cfg(crop_padding=0.0, one_query_per_worker=True), model=m)
    [r] = a.check_workers(IMG, [BOX])
    a.close()
    assert r.unverified == ["helmet"] and r.violations == ["No Vest"] and len(m.questions) == 1


def test_not_visible_flows_through_voting_like_any_unverified_item():
    class Two(Scripted):
        def query(self, image, question):
            self.questions.append(question)
            return {"answer": "visible: no; wearing: no"}
    a = PPEAnalyzer(make_cfg(crop_padding=0.0, required_ppe=["helmet"]), model=Two())
    s = PersonStatus(window=3, confirm=2)
    for frame in range(3):
        [r] = a.check_workers(IMG, [BOX])
        s.add(r, frame)
    a.close()
    shown = s.display(1, BOX, too_small=False)
    # Never a violation, however many "wearing: no" the replies hold: the helmet is unseen.
    assert shown.violations == [] and shown.unverified == ["helmet"]
