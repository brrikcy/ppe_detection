"""Pinned Moondream model and configurable endpoint."""

import time

import pytest
import requests
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

import app.main as main_mod
from app.core.settings import Settings
from app.main import create_app
from app.services.keys import ModelRejected, MoondreamUnreachable, verify_key
from ppe.analyzer import PPEAnalyzer
from ppe.config import endpoint_from_env
from ppe.moondream_client import DEFAULT_MODEL, AuthError, ModelError, MoondreamCloud
from ppe.tuning import Tuning

from .test_pipeline import FakeModel, FakePeople, write_video


class FakeResponse:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._body, self.text = status, body or {"answer": "yes"}, text

    def json(self):
        return self._body


@pytest.fixture
def sent(monkeypatch):
    class Calls(list):
        reply = None

    calls = Calls()

    def post(self, url, json=None, timeout=None):
        calls.append((url, json))
        return calls.reply or FakeResponse()
    monkeypatch.setattr("requests.Session.post", post)
    return calls


IMG = Image.new("RGB", (8, 8))


def test_model_is_sent_to_the_configured_endpoint(sent):
    MoondreamCloud("k", endpoint="https://example.test/v1/", model="moondream3-preview").query(IMG, "q?")
    MoondreamCloud("k").detect(IMG, "hard hat")
    (url1, p1), (url2, p2) = sent
    assert url1 == "https://example.test/v1/query" and p1["model"] == "moondream3-preview"
    assert url2 == "https://api.moondream.ai/v1/detect" and p2["model"] == DEFAULT_MODEL == "moondream3.1-9B-A2B"


def test_no_model_lets_the_server_choose(sent):
    MoondreamCloud("k", model=None).query(IMG, "q?")
    assert "model" not in sent[0][1]


def test_errors_are_classified(sent):
    c = MoondreamCloud("k", model="nope")
    sent.reply = FakeResponse(400, text='{"error":"Invalid model: \\"nope\\". Must be ..."}')
    with pytest.raises(ModelError):
        c.query(IMG, "q?")
    sent.reply = FakeResponse(401, text="Unauthorized")
    with pytest.raises(AuthError):
        c.query(IMG, "q?")
    sent.reply = FakeResponse(400, text="image too large")
    with pytest.raises(RuntimeError) as e:
        c.query(IMG, "q?")
    assert not isinstance(e.value, ModelError)


@pytest.mark.parametrize("value,ok", [("moondream3.1-9B-A2B", True), ("moondream3-preview/ft_abc@100", True),
                                      ("moondream 3", False), ("", False), ("a;b", False)])
def test_model_id_validation(value, ok):
    if ok:
        assert Tuning(moondream_model=value).moondream_model == value
    else:
        with pytest.raises(ValidationError):
            Tuning(moondream_model=value)


def test_endpoint_from_env(monkeypatch):
    monkeypatch.delenv("MOONDREAM_ENDPOINT", raising=False)
    assert endpoint_from_env() == "https://api.moondream.ai/v1"
    monkeypatch.setenv("MOONDREAM_ENDPOINT", " http://localhost:2020/v1/ ")
    assert endpoint_from_env() == "http://localhost:2020/v1"
    monkeypatch.setenv("MOONDREAM_ENDPOINT", "localhost:2020")
    with pytest.raises(ValueError):
        endpoint_from_env()
    monkeypatch.setenv("MOONDREAM_CLOUD_ENDPOINT", "https://other.test/v1")
    assert endpoint_from_env("MOONDREAM_CLOUD_ENDPOINT") == "https://other.test/v1"


def test_analyzer_builds_client_with_endpoint_and_model():
    from ppe.config import Config
    a = PPEAnalyzer(Config(api_key="k", moondream_endpoint="https://x.test/v1",
                           tuning=Tuning(moondream_model="moondream3-preview")))
    assert (a.model.endpoint, a.model.model) == ("https://x.test/v1", "moondream3-preview")
    a.close()


# ---- through the app --------------------------------------------------------------
def make_app(tmp_path, monkeypatch=None, **kw):
    return create_app(Settings(data_dir=tmp_path / "runs", server_api_key="srv",
                               moondream_endpoint="https://self-hosted.test/v1"),
                      people_factory=lambda fps: FakePeople(), **kw)


def upload(c, tmp_path):
    v = write_video(tmp_path / "v.mp4")
    with v.open("rb") as f:
        return c.post("/api/runs", files={"video": ("v.mp4", f, "video/mp4")})


def test_default_key_check_uses_configured_endpoint_and_saved_model(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(main_mod, "verify_key", lambda key, endpoint, model: seen.append((key, endpoint, model)))
    app = make_app(tmp_path, analyzer_factory=lambda cfg: PPEAnalyzer(cfg, model=FakeModel()))
    with TestClient(app) as c:
        s = c.get("/api/dev/tuning").json()
        assert s["moondream_endpoint"] == "https://self-hosted.test/v1"
        c.put("/api/dev/tuning", json={**s["active"], "moondream_model": "moondream3-preview"})
        assert c.post("/api/key/check").json()["ok"] is True
        assert seen[-1] == ("srv", "https://self-hosted.test/v1", "moondream3-preview")


def test_rejected_model_refuses_the_run(tmp_path):
    def verify(key, endpoint):
        raise ModelRejected("The Moondream endpoint doesn't accept model 'x'. Change it in Developer options.")
    app = make_app(tmp_path, key_verifier=verify)
    with TestClient(app) as c:
        r = upload(c, tmp_path)
        assert r.status_code == 422 and "Developer options" in r.json()["detail"]
        assert c.get("/api/runs").json() == []
        assert c.post("/api/key/check").json()["ok"] is False


def test_model_error_mid_run_fails_with_clear_message_and_runs_record_endpoint(tmp_path):
    class Rejects(FakeModel):
        def query(self, image, q):
            raise ModelError("no such model")
    configs = []

    def factory(cfg):
        configs.append(cfg)
        return PPEAnalyzer(cfg, model=Rejects())
    app = make_app(tmp_path, key_verifier=lambda k, e: None, analyzer_factory=factory)
    with TestClient(app) as c:
        rid = upload(c, tmp_path).json()["meta"]["id"]
        for _ in range(200):
            m = c.get(f"/api/runs/{rid}").json()["meta"]
            if m["status"] in ("completed", "failed"):
                break
            time.sleep(0.05)
    assert m["status"] == "failed" and "moondream3.1-9B-A2B" in m["error"] and "Developer options" in m["error"]
    assert m["moondream_endpoint"] == "https://self-hosted.test/v1"
    assert configs[0].moondream_endpoint == "https://self-hosted.test/v1"


def test_entered_key_uses_moondream_cloud_and_server_key_uses_server_endpoint(tmp_path):
    checked, configs = [], []

    def factory(cfg):
        configs.append(cfg)
        return PPEAnalyzer(cfg, model=FakeModel())
    app = make_app(tmp_path, key_verifier=lambda k, e: checked.append((k, e)), analyzer_factory=factory)
    with TestClient(app) as c:
        v = write_video(tmp_path / "v.mp4")
        with v.open("rb") as f:
            entered = c.post("/api/runs", files={"video": ("v.mp4", f, "video/mp4")}, data={"api_key": "mine"})
        server = upload(c, tmp_path)
        assert c.post("/api/key/check", data={"api_key": "mine"}).json()["ok"] is True
        for _ in range(200):
            if all(r["status"] in ("completed", "failed") for r in c.get("/api/runs").json()):
                break
            time.sleep(0.05)
    assert entered.json()["meta"]["moondream_endpoint"] == "https://api.moondream.ai/v1"
    assert server.json()["meta"]["moondream_endpoint"] == "https://self-hosted.test/v1"
    assert checked == [("mine", "https://api.moondream.ai/v1"), ("srv", "https://self-hosted.test/v1"),
                       ("mine", "https://api.moondream.ai/v1")]
    assert sorted((cfg.api_key, cfg.moondream_endpoint) for cfg in configs) == [
        ("mine", "https://api.moondream.ai/v1"), ("srv", "https://self-hosted.test/v1")]


def test_local_moondream_is_checked_by_health_not_by_a_query(sent, monkeypatch):
    gets = []

    class Ok:
        def raise_for_status(self):
            pass

    monkeypatch.setattr("requests.get", lambda url, timeout=None: gets.append(url) or Ok())
    verify_key("local", "http://127.0.0.1:2020/v1")
    assert gets == ["http://127.0.0.1:2020/health"] and sent == []  # nothing waited for the GPU

    def down(url, timeout=None):
        raise requests.ConnectionError("refused")
    monkeypatch.setattr("requests.get", down)
    with pytest.raises(MoondreamUnreachable):
        verify_key("local", "http://localhost:2020/v1")

    verify_key("k", "https://self-hosted.test/v1")
    assert sent[0][0] == "https://self-hosted.test/v1/query"
