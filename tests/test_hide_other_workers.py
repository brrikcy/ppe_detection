"""Hide other workers: outlines from a segmentation model, greyed out in a worker's crop."""

import json
import time
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.core.settings import Settings
from app.main import create_app
from ppe.analyzer import PPEAnalyzer, WorkerResult, hide_others
from ppe.config import Config
from ppe.people import TrackedPerson
from ppe.tuning import Tuning
from ppe.video import process_video

from .test_pipeline import write_video


def rect(x1, y1, x2, y2):
    return np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]], np.float32)


def grey(pixel, tolerance=0):
    return bool(np.abs(np.asarray(pixel, int) - 128).max() <= tolerance)


# ---- greying out the other workers -----------------------------------------------------------
def test_hide_others_greys_neighbours_but_never_this_worker():
    frame = Image.fromarray(np.full((100, 200, 3), 255, np.uint8))
    own = rect(20, 10, 90, 90)  # this worker's silhouette
    other = rect(60, 10, 190, 90)  # a neighbour overlapping the worker's right side
    out, hidden = hide_others(frame, (0, 0, 200, 100), own, [other])
    a = np.asarray(out)
    assert hidden == 1
    assert grey(a[50, 150])  # the neighbour, away from this worker: hidden
    assert (a[50, 70] == 255).all()  # where they overlap this worker: this worker's pixels stay
    assert (a[50, 30] == 255).all()  # this worker
    assert (np.asarray(frame) == 255).all()  # the original is untouched


def test_hide_others_uses_frame_coordinates_for_a_crop():
    frame = Image.fromarray(np.full((100, 200, 3), 255, np.uint8))
    box = (50, 0, 200, 100)
    out, hidden = hide_others(frame.crop(box), box, rect(55, 10, 100, 90), [rect(120, 10, 190, 90)])
    a = np.asarray(out)
    assert hidden == 1 and grey(a[50, 150 - 50]) and (a[50, 70 - 50] == 255).all()


def test_a_neighbour_outside_the_crop_changes_nothing():
    crop = Image.fromarray(np.full((50, 50, 3), 255, np.uint8))
    out, hidden = hide_others(crop, (0, 0, 50, 50), rect(5, 5, 40, 45), [rect(300, 0, 340, 50)])
    assert hidden == 0 and out is crop


# ---- in the analyzer -----------------------------------------------------------------------------
class Recording:
    """Answers "no" to everything and keeps the images it was shown."""

    def __init__(self):
        self.images = []

    def query(self, image, question):
        self.images.append(np.asarray(image).copy())
        return {"answer": "visible: yes; wearing: no"}

    def detect(self, image, obj):
        return {"objects": []}


FRAME = Image.fromarray(np.full((200, 400, 3), 255, np.uint8))
A, B = (20, 40, 160, 190), (120, 40, 280, 190)  # B overlaps A's right part; crops are 140 and 160 px wide
A_MASK, B_MASK = rect(40, 45, 110, 188), rect(130, 45, 275, 188)


def run_check(masks=(A_MASK, B_MASK), **tuning):
    model = Recording()
    cfg = Config(api_key="x", required_ppe=["helmet"],
                 tuning=Tuning(crop_padding=0.0, min_check_height_px=0, min_check_height_frac=0, **tuning))
    a = PPEAnalyzer(cfg, model=model)
    results = a.check_workers(FRAME, [A, B], [A, B], list(masks))
    a.close()
    shown = {im.shape[1]: im for im in model.images}  # by crop width: A is 140 px wide, B 160
    return shown[140], shown[160], results


def test_the_other_worker_is_hidden_in_a_workers_crop():
    a_img, b_img, results = run_check(hide_other_workers=True)
    assert grey(a_img[60, 145 - 20])  # B's body inside A's crop, beyond A's own outline
    assert (a_img[60, 70 - 20] == 255).all()  # A themselves
    assert (b_img == 255).all()  # A's outline is outside B's crop: nothing to hide
    assert [r.violations for r in results] == [["No Helmet"], ["No Helmet"]]


def test_nothing_is_hidden_unless_the_setting_is_on():
    a_img, b_img, _ = run_check(hide_other_workers=False)
    assert (a_img == 255).all() and (b_img == 255).all()
    assert Tuning().hide_other_workers is False


def test_nothing_is_hidden_without_outlines():
    # A normal detector gives no outlines: crops are as before.
    a_img, b_img, _ = run_check(masks=(None, None), hide_other_workers=True)
    assert (a_img == 255).all() and (b_img == 255).all()
    # Hiding needs this worker's own outline too, so their pixels can't be lost.
    a_img, _, _ = run_check(masks=(None, B_MASK), hide_other_workers=True)
    assert (a_img == 255).all()


def test_explain_item_hides_the_same_way_and_says_so():
    model = Recording()
    a = PPEAnalyzer(Config(api_key="x", required_ppe=["helmet"], tuning=Tuning(crop_padding=0.0, hide_other_workers=True)),
                    model=model)
    out = a.explain_item(FRAME, "helmet", A, [B], A_MASK, [B_MASK])
    plain = a.explain_item(FRAME, "helmet", A, [B])
    a.close()
    assert out["hidden_workers"] == 1 and grey(np.asarray(out["crop"])[60, 145 - 20])
    assert plain["hidden_workers"] == 0 and (np.asarray(plain["crop"]) == 255).all()


def test_an_outline_does_not_take_part_in_comparing_results():
    one = WorkerResult(box=(0, 0, 1, 1), mask=np.zeros((3, 2), np.float32))
    two = WorkerResult(box=(0, 0, 1, 1), mask=np.ones((3, 2), np.float32))
    assert one == two and "mask" not in repr(one)


# ---- the tracker keeps each person's outline ------------------------------------------------------
class FakeSeg:
    """Stands in for a segmentation YOLO model: returns the detections it is given."""

    def __init__(self):
        self.detections = []  # (box, outline)

    def predict(self, frame, **kwargs):
        from ultralytics.engine.results import Boxes

        data = np.array([[*box, 0.9, 0] for box, _ in self.detections], np.float32).reshape(-1, 6)
        h, w = frame.shape[:2]
        return [SimpleNamespace(boxes=Boxes(data, (h, w)), masks=SimpleNamespace(xy=[o for _, o in self.detections]))]


@pytest.fixture
def plain_pillow(monkeypatch):
    """Importing Ultralytics wraps PIL.Image.open (HEIF support). Put Pillow back after the test, so
    other tests in this run see the plain one."""
    monkeypatch.setattr(Image, "open", Image.open)


def tracker(monkeypatch, model, **kwargs):
    import ppe.people as people

    monkeypatch.setattr(people, "_load_model", lambda path: model)
    return people.YoloPersonTracker(fps=10, min_age_sec=0, coast_sec=0, fixed_camera=True, **kwargs)


def test_each_tracked_person_gets_the_outline_of_their_own_detection(monkeypatch, plain_pillow):
    model = FakeSeg()
    t = tracker(monkeypatch, model, detect_fps=0)
    frame = np.zeros((300, 600, 3), np.uint8)
    left, right = ((50, 50, 150, 250), rect(60, 60, 140, 240)), ((400, 50, 500, 250), rect(410, 60, 490, 240))
    for order in ([left, right], [right, left], [left, right]):  # the detector's order changes between frames
        model.detections = order
        people = t.update(frame)
        assert len(people) == 2
        for p in people:
            expected = left[1] if p.box[0] < 300 else right[1]
            assert p.mask is not None and np.allclose(p.mask, expected)


def test_no_outline_without_a_segmentation_model(monkeypatch, plain_pillow):
    model = FakeSeg()
    model.predict_plain = model.predict

    def plain(frame, **kwargs):
        [r] = model.predict_plain(frame)
        return [SimpleNamespace(boxes=r.boxes, masks=None)]
    model.predict = plain
    model.detections = [((50, 50, 150, 250), rect(60, 60, 140, 240))]
    [p] = tracker(monkeypatch, model, detect_fps=0).update(np.zeros((300, 600, 3), np.uint8))
    assert p.mask is None


def test_the_outline_moves_with_the_box_between_detections(monkeypatch, plain_pillow):
    model = FakeSeg()
    t = tracker(monkeypatch, model, detect_fps=5)  # detects on every 2nd frame
    frame = np.zeros((300, 600, 3), np.uint8)
    seen = []
    for x in [100, None, 120, None, 140, None]:
        if x is not None:
            model.detections = [((x, 50, x + 100, 250), rect(x + 10, 60, x + 90, 240))]
        [p] = t.update(frame)
        seen.append(p)
    detected, moved = seen[4], seen[5]
    assert moved.box[0] > detected.box[0]
    assert np.allclose(moved.mask - detected.mask, [moved.box[0] - detected.box[0], moved.box[1] - detected.box[1]])


# ---- in a run ---------------------------------------------------------------------------------------
class MaskedPeople:
    """Two overlapping workers (ids 1 and 2) with outlines, in the 400x200 test video."""

    def update(self, frame):
        return [TrackedPerson(id=1, box=A, score=0.9, mask=A_MASK), TrackedPerson(id=2, box=B, score=0.9, mask=B_MASK)]


def test_a_run_hides_the_other_worker_and_keeps_outlines_in_the_report(tmp_path):
    src = write_video(tmp_path / "in.mp4", frames=12, fps=10)  # left half black, right half bright
    model = Recording()
    cfg = Config(api_key="x", required_ppe=["helmet"],
                 tuning=Tuning(crop_padding=0.0, min_check_height_px=0, min_check_height_frac=0, hide_other_workers=True))
    a = PPEAnalyzer(cfg, model=model)
    report = process_video(src, tmp_path / "out.mp4", a, people_factory=lambda fps: MaskedPeople())
    a.close()
    # B's body inside A's crop was greyed out (the video is black there).
    a_images = [im for im in model.images if im.shape[1] == 140]
    assert a_images and all(grey(im[60, 145 - 20], tolerance=16) for im in a_images)
    # Outlines are saved with each worker, as whole-pixel polygons.
    workers = [w for e in report["timeline"] for w in e["workers"]]
    assert workers and all(len(w["mask"]) >= 3 for w in workers)
    assert all(isinstance(v, int) for w in workers for pt in w["mask"] for v in pt)


@pytest.fixture
def client(tmp_path):
    def factory(cfg):
        return PPEAnalyzer(cfg, model=Recording())

    app = create_app(Settings(data_dir=tmp_path / "runs", server_api_key="srv"), analyzer_factory=factory,
                     key_verifier=lambda k, e: None, people_factory=lambda fps: MaskedPeople())
    with TestClient(app) as c:
        c.tmp = tmp_path
        yield c


def test_the_prompt_playground_repeats_a_check_with_the_neighbour_hidden(client):
    active = client.get("/api/dev/tuning").json()["active"]
    src = write_video(client.tmp / "v.mp4", frames=25, fps=10)
    with src.open("rb") as f:
        rid = client.post("/api/runs", files={"video": ("v.mp4", f, "video/mp4")}).json()["meta"]["id"]
    for _ in range(200):
        d = client.get(f"/api/runs/{rid}").json()
        if d["meta"]["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert d["meta"]["status"] == "completed"
    assert client.get(f"/api/runs/{rid}/report").json()["timeline"][0]["workers"][0]["mask"]
    snap = next(e["snapshot"] for e in d["summary"]["events"] if e["track_id"] == 1)

    def recheck(hide):
        tuning = {**active, "hide_other_workers": hide, "crop_padding": 0.0}
        r = client.post("/api/dev/try", data={"item": "helmet", "run_id": rid, "folder": "snapshots", "name": snap,
                                              "tuning": json.dumps(tuning)})
        assert r.status_code == 200, r.text
        return r.json()

    assert "1 other worker hidden" in recheck(True)["note"]
    assert "hidden" not in recheck(False)["note"]
