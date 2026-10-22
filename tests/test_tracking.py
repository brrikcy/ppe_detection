"""PersonTracker on synthetic video: moving coloured blocks over a textured background."""

import numpy as np

from ppe.tracking import PersonTracker

W, H = 640, 360
rng = np.random.default_rng(0)
BACKGROUND = rng.integers(0, 255, (H, W, 3), dtype=np.uint8)  # texture gives optical flow corners
COLORS = {"a": (0, 200, 255), "b": (255, 80, 0), "c": (60, 220, 60)}


def frame_with(blocks):
    f = BACKGROUND.copy()
    for name, (x, y) in blocks.items():
        f[y:y + 120, x:x + 50] = COLORS[name]
        f[y + 10:y + 20, x + 10:x + 40] = 255  # some texture inside the block too
    return f


def boxes(blocks):
    return [(x, y, x + 50, y + 120) for x, y in blocks.values()]


def test_ids_stay_stable_while_people_move():
    t = PersonTracker()
    seen = []
    for i in range(60):
        blocks = {"a": (40 + 3 * i, 100), "b": (500 - 3 * i, 180)}
        f = frame_with(blocks)
        t.propagate(f)
        if i % 10 == 0:
            seen.append(t.update(f, boxes(blocks)))
    assert all(ids == [1, 2] for ids in seen)


def test_new_person_gets_new_id_and_brief_absence_keeps_id():
    t = PersonTracker(max_missed=3)
    schedule = [
        {"a": (100, 100)},
        {"a": (110, 100), "c": (400, 150)},  # c appears
        {"c": (405, 150)},  # a hidden
        {"c": (410, 150)},  # a still hidden
        {"a": (130, 100), "c": (415, 150)},  # a is back
    ]
    results = []
    for blocks in schedule:
        f = frame_with(blocks)
        t.propagate(f)
        results.append(dict(zip(blocks, t.update(f, boxes(blocks)))))
    assert results[0] == {"a": 1}
    assert results[1] == {"a": 1, "c": 2}
    assert results[4] == {"a": 1, "c": 2}
