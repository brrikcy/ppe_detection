"""Offline tests using a fake Moondream model and a fake person tracker (no API key, no YOLO)."""

import json

import cv2
import numpy as np
from PIL import Image

from ppe.analyzer import PPEAnalyzer, WorkerResult
from ppe.annotate import draw_results
from ppe.config import Config
from ppe.people import TrackedPerson
from ppe.status import PersonStatus
from ppe.video import process_video

# The two workers in the synthetic 400x200 frames: left has everything, right has nothing.
LEFT, RIGHT = (20, 40, 140, 190), (220, 40, 340, 190)


class FakeModel:
    """Crops of the right-hand worker are bright (value 200): they get "no" and no PPE found."""

    HEAD = {"x_min": 0.3, "y_min": 0.0, "x_max": 0.7, "y_max": 0.2}

    def detect(self, image, obj):
        right = np.asarray(image).mean() > 150
        return {"objects": [self.HEAD] if right and obj == "head" else []}

    def query(self, image, question):
        right = np.asarray(image).mean() > 150
        return {"answer": "No." if right else "Yes"}


class FakePeople:
    """Stands in for YoloPersonTracker: a fixed schedule of people per frame."""

    def __init__(self, schedule=lambda frame_idx: [(1, LEFT), (2, RIGHT)]):
        self.schedule, self.i = schedule, 0

    def update(self, frame):
        people = [TrackedPerson(id=tid, box=box, score=0.9) for tid, box in self.schedule(self.i)]
        self.i += 1
        return people


def make_cfg(**kw):
    return Config(api_key="test", **kw)


def frame():
    img = np.zeros((200, 400, 3), np.uint8)
    img[:, 200:] = 200
    return img


def write_video(path, frames=25, fps=10):
    w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (400, 200))
    for _ in range(frames):
        w.write(frame())
    w.release()
    return path


def test_labels_and_tags():
    assert WorkerResult((0, 0, 1, 1)).label == "PPE OK"
    r = WorkerResult((0, 0, 1, 1), ["No Helmet", "No Vest"], track_id=3)
    assert r.label == "PPE Violation (2) - No Helmet, No Vest"
    assert r.tag == "#3 Missing: Helmet, Vest"
    assert WorkerResult((0, 0, 1, 1), unverified=["helmet"], track_id=1).tag == "#1 OK? Helmet unseen"
    assert WorkerResult((0, 0, 1, 1), pending=True, track_id=4).tag == "#4 Checking..."


def test_check_workers():
    a = PPEAnalyzer(make_cfg(crop_padding=0.0), model=FakeModel())
    results = a.check_workers(Image.fromarray(frame()), [LEFT, RIGHT])
    a.close()
    assert [r.label for r in results] == ["PPE OK", "PPE Violation (2) - No Helmet, No Vest"]


class OccludedModel:
    """Query says "no" for everything, but detect finds a vest and no head."""

    def detect(self, image, obj):
        box = {"x_min": 0.1, "y_min": 0.3, "x_max": 0.9, "y_max": 0.6}
        return {"objects": [box] if obj == "high visibility vest" else []}

    def query(self, image, question):
        return {"answer": "no"}


def test_no_answer_is_confirmed_with_detect():
    a = PPEAnalyzer(make_cfg(), model=OccludedModel())
    [r] = a.check_workers(Image.fromarray(np.zeros((200, 400, 3), np.uint8)), [(40, 0, 200, 180)])
    a.close()
    # Vest found by detect -> present; head not visible -> helmet unverified.
    assert r.label == "PPE OK"
    assert r.unverified == ["helmet"]


class NeighbourVestModel:
    """Two overlapping workers; only the right one wears a vest, and it shows in the left one's crop.

    The query says "no" for the left worker, and detect() finds the right worker's vest in
    every crop. That vest must not count for the left worker.
    """

    BOXES = [(80, 20, 200, 190), (160, 20, 280, 190)]
    VEST_PX = (220, 70, 270, 130)  # on the right worker's torso, in frame pixels (400x200)

    def detect(self, image, obj):
        if obj == "high visibility vest":
            # Crops equal the person boxes (no padding): left starts at x=80, right at x=160.
            ox, oy = (80 if self._left(image) else 160), 20
            x1, y1, x2, y2 = self.VEST_PX
            w, h = image.size
            return {"objects": [{"x_min": (x1 - ox) / w, "y_min": (y1 - oy) / h,
                                 "x_max": (x2 - ox) / w, "y_max": (y2 - oy) / h}]}
        return {"objects": []}

    def query(self, image, question):
        return {"answer": "no" if self._left(image) else "yes"}

    @staticmethod
    def _left(image):
        return np.asarray(image)[0, 0, 0] == 10  # the frame is dark left of x=160


def test_neighbours_vest_does_not_count():
    img = np.full((200, 400, 3), 200, np.uint8)
    img[:, :160] = 10  # marks crops that start on the left worker
    a = PPEAnalyzer(make_cfg(crop_padding=0.0, required_ppe=["vest"]), model=NeighbourVestModel())
    left, right = a.check_workers(Image.fromarray(img), NeighbourVestModel.BOXES)
    a.close()
    assert left.violations == ["No Vest"]
    assert right.ok


def test_far_away_workers_are_not_checked():
    img = np.zeros((1000, 400, 3), np.uint8)  # the second worker is 50px of a 1000px frame
    a = PPEAnalyzer(make_cfg(crop_padding=0.0), model=FakeModel())
    near, far = a.check_workers(Image.fromarray(img), [(20, 200, 140, 950), (320, 100, 330, 150)])
    a.close()
    assert not near.too_small and far.too_small
    assert far.label == "Too far to check" and far.violations == []


def test_status_vote_ignores_a_single_misread():
    s = PersonStatus()
    box = (0, 0, 10, 10)
    seq = [[], ["No Vest"], [], ["No Vest"], ["No Vest"], [], []]
    shown = []
    for i, v in enumerate(seq):
        s.add(WorkerResult(box, list(v)), i)
        shown.append(s.display(1, box, too_small=False).violations)
    # One bad check after a good one is a tie -> stays OK; two bad in the last three -> shown.
    assert shown == [[], [], [], ["No Vest"], ["No Vest"], ["No Vest"], []]
    assert PersonStatus().display(2, box, too_small=False).pending  # not checked yet


def test_process_video(tmp_path):
    src = write_video(tmp_path / "in.mp4")
    a = PPEAnalyzer(make_cfg(crop_padding=0.0, analysis_interval_sec=1.0), model=FakeModel())
    out = tmp_path / "out.mp4"
    report = process_video(src, out, a, people_factory=lambda fps: FakePeople())
    a.close()

    assert out.exists() and out.stat().st_size > 0
    assert report["frames_written"] == 25
    assert report["frames_analysed"] == 3  # frames 0, 10, 20
    data = json.loads(out.with_suffix(".json").read_text())
    assert data["timeline"][0]["workers"][1]["violations"] == ["No Helmet", "No Vest"]
    assert [[w["track_id"] for w in e["workers"]] for e in data["timeline"]] == [[1, 2]] * 3


def test_newcomer_is_checked_on_arrival(tmp_path):
    # Worker 2 walks in at frame 2; checks are every 10 frames, so they're checked straight away.
    src = write_video(tmp_path / "in.mp4")
    calls = []

    class Counting(FakeModel):
        def query(self, image, question):
            calls.append(np.asarray(image).mean() > 150)
            return super().query(image, question)

    a = PPEAnalyzer(make_cfg(crop_padding=0.0, required_ppe=["helmet"]), model=Counting())
    people = FakePeople(lambda i: [(1, LEFT)] + ([(2, RIGHT)] if i >= 2 else []))
    report = process_video(src, tmp_path / "out.mp4", a, people_factory=lambda fps: people)
    a.close()
    # Right-hand queries: frame 2 (on arrival), then frames 10 and 20.
    assert sum(calls) == 3
    # The arrival check is in the report, holding only the newcomer.
    arrivals = [e for e in report["timeline"] if e["arrival"]]
    assert [(e["frame"], [w["track_id"] for w in e["workers"]]) for e in arrivals] == [(2, [2])]
    assert arrivals[0]["workers"][0]["violations"] == ["No Helmet"]
    assert report["frames_analysed"] == 3 and report["arrival_checks"] == 1


def test_worker_who_leaves_before_next_check_is_reported(tmp_path):
    # Worker 2 is only on screen for frames 2-6, between the checks at 0 and 10.
    src = write_video(tmp_path / "in.mp4")
    a = PPEAnalyzer(make_cfg(crop_padding=0.0, required_ppe=["helmet"]), model=FakeModel())
    people = FakePeople(lambda i: [(1, LEFT)] + ([(2, RIGHT)] if 2 <= i <= 6 else []))
    report = process_video(src, tmp_path / "out.mp4", a, people_factory=lambda fps: people)
    a.close()

    from app.services.summary import build_summary

    s = build_summary(report["timeline"])
    people_ids = {p.id: p for p in s.people}
    assert people_ids[2].checks == 1 and people_ids[2].status_label == "PPE Violation (1) - No Helmet"
    assert [e.track_id for e in s.events] == [2]
    assert s.frames_analysed == 3 and s.arrival_checks == 1
    # The timeline chart only has the regular checks, where worker 2 isn't on screen.
    assert [(p.time_sec, p.workers, p.violations) for p in s.timeline] == [(0, 1, 0), (1, 1, 0), (2, 1, 0)]


def test_failed_check_is_skipped_not_repeated(tmp_path):
    src = write_video(tmp_path / "in.mp4")

    class FlakyOnce(FakeModel):
        failed = False

        def query(self, image, question):
            if not FlakyOnce.failed:
                FlakyOnce.failed = True
                raise RuntimeError("HTTP 500")
            return super().query(image, question)

    import ppe.analyzer as analyzer_mod
    real_retries = analyzer_mod._with_retries
    analyzer_mod._with_retries = lambda fn, *a, **k: fn(*a, **k)  # no retry/backoff in this test
    try:
        a = PPEAnalyzer(make_cfg(crop_padding=0.0, required_ppe=["helmet"], max_workers=1), model=FlakyOnce())
        report = process_video(src, tmp_path / "out.mp4", a, people_factory=lambda fps: FakePeople())
        a.close()
    finally:
        analyzer_mod._with_retries = real_retries
    # The first check failed, so there is no entry for frame 0 (rather than a stale copy).
    # Nobody has a verdict yet, so frame 1 checks them as newcomers.
    assert [(e["frame"], e["arrival"]) for e in report["timeline"]] == [(1, True), (10, False), (20, False)]


def test_tags_do_not_overlap():
    img = np.zeros((300, 600, 3), np.uint8)
    # Two workers whose boxes start at nearly the same height and overlap horizontally.
    a = WorkerResult((100, 80, 260, 290), ["No Helmet", "No Vest"], track_id=1)
    b = WorkerResult((150, 85, 300, 290), [], track_id=2)
    draw_results(img, [a, b])
    # Each tag is a solid filled band; count the rows that carry tag colour at x=200 and
    # check there are two separate bands (one per tag) rather than one shared band.
    col = img[:, 200]
    red = (col == (30, 30, 220)).all(axis=1)
    green = (col == (40, 180, 40)).all(axis=1)
    red_rows, green_rows = set(np.flatnonzero(red)), set(np.flatnonzero(green))
    assert len(red_rows) > 5 and len(green_rows) > 5 and not (red_rows & green_rows)
