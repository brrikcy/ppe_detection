"""Offline tests using a fake Moondream model (no API key needed)."""

import json

import cv2
import numpy as np
from PIL import Image

from ppe.analyzer import PPEAnalyzer, WorkerResult, _dedupe
from ppe.config import Config
from ppe.video import process_video


class FakeModel:
    """Two people: left one has everything, right one has no helmet and no vest."""

    HEAD = {"x_min": 0.3, "y_min": 0.0, "x_max": 0.7, "y_max": 0.2}

    def detect(self, image, obj):
        if obj != "person":
            # On crops: the right worker has a visible head but no PPE.
            right = np.asarray(image).mean() > 150
            return {"objects": [self.HEAD] if right and obj == "head" else []}
        return {"objects": [
            {"x_min": 0.05, "y_min": 0.2, "x_max": 0.35, "y_max": 0.95},
            {"x_min": 0.55, "y_min": 0.2, "x_max": 0.85, "y_max": 0.95},
        ]}

    def query(self, image, question):
        # Crops of the right-hand worker are filled with value 200.
        right = np.asarray(image).mean() > 150
        return {"answer": "No." if right else "Yes"}


def make_cfg(**kw):
    return Config(api_key="test", **kw)


def test_labels():
    assert WorkerResult((0, 0, 1, 1)).label == "PPE OK"
    r = WorkerResult((0, 0, 1, 1), ["No Helmet", "No Vest"])
    assert r.label == "PPE Violation (2) - No Helmet, No Vest"


def test_analyze_frame():
    img = np.zeros((200, 400, 3), np.uint8)
    img[:, 200:] = 200
    a = PPEAnalyzer(make_cfg(crop_padding=0.0), model=FakeModel())
    results = a.analyze(Image.fromarray(img))
    a.close()
    assert [r.label for r in results] == ["PPE OK", "PPE Violation (2) - No Helmet, No Vest"]
    assert results[0].box == (20, 40, 140, 190)


class OccludedModel:
    """Query says "no" for everything, but detect finds a vest and no head."""

    def detect(self, image, obj):
        if obj == "person":
            return {"objects": [{"x_min": 0.1, "y_min": 0.0, "x_max": 0.5, "y_max": 0.9}]}
        box = {"x_min": 0.1, "y_min": 0.3, "x_max": 0.9, "y_max": 0.6}
        return {"objects": [box] if obj == "high visibility vest" else []}

    def query(self, image, question):
        return {"answer": "no"}


def test_no_answer_is_confirmed_with_detect():
    a = PPEAnalyzer(make_cfg(), model=OccludedModel())
    [r] = a.analyze(Image.fromarray(np.zeros((200, 400, 3), np.uint8)))
    a.close()
    # Vest found by detect -> present; head not visible -> helmet unverified.
    assert r.label == "PPE OK"
    assert r.unverified == ["helmet"]


def test_dedupe_drops_contained_boxes():
    assert _dedupe([(0, 0, 100, 200), (10, 50, 90, 150), (200, 0, 260, 120)]) == [
        (0, 0, 100, 200), (200, 0, 260, 120)]


def test_process_video(tmp_path):
    src = tmp_path / "in.mp4"
    w = cv2.VideoWriter(str(src), cv2.VideoWriter_fourcc(*"mp4v"), 10, (400, 200))
    for _ in range(25):
        f = np.zeros((200, 400, 3), np.uint8)
        f[:, 200:] = 200
        w.write(f)
    w.release()

    a = PPEAnalyzer(make_cfg(crop_padding=0.0, analysis_interval_sec=1.0), model=FakeModel())
    out = tmp_path / "out.mp4"
    report = process_video(src, out, a)
    a.close()

    assert out.exists() and out.stat().st_size > 0
    assert report["frames_written"] == 25
    assert report["frames_analysed"] == 3  # frames 0, 10, 20
    data = json.loads(out.with_suffix(".json").read_text())
    assert data["timeline"][0]["workers"][1]["violations"] == ["No Helmet", "No Vest"]
