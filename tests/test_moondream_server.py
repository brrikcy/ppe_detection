"""The local Moondream server speaks the Cloud API that ppe.moondream_client sends (fake model, no GPU)."""

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

from moondream_server.serve import create_app
from ppe.analyzer import PPEAnalyzer
from ppe.moondream_client import MoondreamCloud

from .test_pipeline import LEFT, RIGHT, FakeModel, frame, make_cfg


class ServerModel:
    """FakeModel behind the server's interface: answers as strings, boxes as lists."""

    def __init__(self):
        self.fake, self.questions = FakeModel(), []

    def query(self, image, question):
        self.questions.append(question)
        return self.fake.query(image, question)["answer"]

    def detect(self, image, obj):
        return self.fake.detect(image, obj)["objects"]


def local_client(model) -> MoondreamCloud:
    client = MoondreamCloud("local", endpoint="http://testserver/v1")
    http = TestClient(create_app(model))
    http.headers.update(client.session.headers)
    client.session = http  # same post(url, json=, timeout=) as requests
    return client


def test_pipeline_runs_against_the_local_server():
    model = ServerModel()
    a = PPEAnalyzer(make_cfg(crop_padding=0.0), model=local_client(model))
    results = a.check_workers(Image.fromarray(frame()), [LEFT, RIGHT])
    a.close()
    assert [r.label for r in results] == ["PPE OK", "PPE Violation (2) - No Helmet, No Vest"]
    assert any("hard hat" in q for q in model.questions)


def test_bad_image_is_rejected():
    http = TestClient(create_app(ServerModel()))
    r = http.post("/v1/query", json={"image_url": "https://example.com/a.jpg", "question": "hi"})
    assert r.status_code == 422
    r = http.post("/v1/detect", json={"image_url": "data:image/jpeg;base64,bm90IGFuIGltYWdl", "object": "x"})
    assert r.status_code == 422
    assert http.get("/health").json()["ok"] is True


def test_detect_boxes_round_trip():
    client = local_client(ServerModel())
    bright = Image.fromarray(np.full((100, 100, 3), 200, np.uint8))
    assert client.detect(bright, "head") == {"objects": [FakeModel.HEAD]}
    assert client.query(bright, "Is this person wearing a vest?") == {"answer": "No."}
