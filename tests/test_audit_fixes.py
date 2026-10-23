"""Regression tests for the tracking, detection and voting audit (finding IDs in brackets)."""

import numpy as np
import pytest
from PIL import Image

from ppe.analyzer import PPEAnalyzer, WorkerResult, _is_yes, _parse_multi
from ppe.status import PersonStatus
from ppe.tuning import Tuning
from ppe.video import process_video

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
    assert Down.calls == 6  # 3 check frames x 2 workers, no retries in between


# ---- [S4] [T2] scheduling --------------------------------------------------------------------
def queried_frames(tmp_path, schedule, interval=1.0, frames=25, **tuning):
    frames_seen = []

    class Counting(FakeModel):
        def query(self, image, question):
            if np.asarray(image).mean() > 150:  # right-hand worker
                frames_seen.append(people.i - 1)
            return super().query(image, question)
    people = FakePeople(schedule)
    src = write_video(tmp_path / "in.mp4", frames=frames)
    a = PPEAnalyzer(make_cfg(crop_padding=0.0, required_ppe=["helmet"], analysis_interval_sec=interval,
                             parallel_requests=1, **tuning), model=Counting())
    process_video(src, tmp_path / "out.mp4", a, people_factory=lambda fps: people)
    a.close()
    return frames_seen


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
