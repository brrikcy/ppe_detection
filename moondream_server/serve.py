"""Local Moondream server with the Moondream Cloud REST API (POST /v1/query, /v1/detect).

Runs Moondream on this machine's GPU, so PPE checks don't go over the internet:

    pip install -r requirements-gpu.txt
    python -m moondream_server.serve            # http://127.0.0.1:2020/v1

then in .env: MOONDREAM_ENDPOINT=http://127.0.0.1:2020/v1 and MOONDREAM_API_KEY=local (any
value; the key isn't checked here). The pipeline's client (ppe.moondream_client) is unchanged.

Backends:
  photon  Moondream's own inference engine (`pip install moondream`); the same
          moondream3.1-9B-A2B model as Moondream Cloud. Needs an NVIDIA Ampere or newer GPU
          (A10G, L4, L40S, A100...), not a T4. No API key for the base model.
  hf      Hugging Face transformers (moondream/moondream3-preview); a fallback if Photon
          won't install. Slower.

The server listens on localhost only by default: it has no authentication.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import io
import logging
import threading
import time

from fastapi import FastAPI, HTTPException
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel

log = logging.getLogger("moondream_server")

DEFAULT_MODELS = {"photon": "moondream3.1-9B-A2B", "hf": "moondream/moondream3-preview"}


class QueryRequest(BaseModel):
    image_url: str
    question: str
    stream: bool = False
    model: str | None = None  # sent by the client; this server runs one model, so it's ignored


class DetectRequest(BaseModel):
    image_url: str
    object: str
    model: str | None = None


def decode_image(url: str) -> Image.Image:
    """A data: URI (what the client sends) -> RGB image."""
    if not url.startswith("data:") or "," not in url:
        raise HTTPException(422, "image_url must be a base64 data: URI")
    try:
        return Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).convert("RGB")
    except (binascii.Error, UnidentifiedImageError, OSError) as exc:
        raise HTTPException(422, f"Couldn't read the image: {exc}")


class Model:
    """query/detect on a loaded Moondream, at most `concurrency` calls at a time."""

    def __init__(self, backend: str, model_id: str | None = None, concurrency: int = 1):
        self.backend, self.model_id = backend, model_id or DEFAULT_MODELS[backend]
        self._slots = threading.Semaphore(concurrency)
        log.info("Loading %s with %s...", self.model_id, backend)
        t = time.perf_counter()
        if backend == "photon":
            import moondream as md

            self._md = md.photon(self.model_id)
            self._query = lambda image, q: self._md.query(image, q)
        elif backend == "hf":
            import torch
            from transformers import AutoModelForCausalLM

            self._md = AutoModelForCausalLM.from_pretrained(
                self.model_id, trust_remote_code=True, dtype=torch.bfloat16, device_map={"": "cuda"})
            self._md.compile()  # also warms up
            # Short yes/no answers: no reasoning pass (the transformers model reasons by default).
            self._query = lambda image, q: self._md.query(image=image, question=q, reasoning=False)
        else:
            raise ValueError(f"unknown backend {backend!r}; use photon or hf")
        log.info("Loaded in %.0fs", time.perf_counter() - t)

    def _run(self, name: str, fn, *args):
        with self._slots:
            t = time.perf_counter()
            out = fn(*args)
        log.debug("%s %.0f ms", name, (time.perf_counter() - t) * 1000)
        return out

    def query(self, image: Image.Image, question: str) -> str:
        return self._run("query", self._query, image, question)["answer"]

    def detect(self, image: Image.Image, obj: str) -> list[dict]:
        objects = self._run("detect", self._md.detect, image, obj)["objects"]
        return [{k: float(o[k]) for k in ("x_min", "y_min", "x_max", "y_max")} for o in objects]


def create_app(model) -> FastAPI:
    """`model` has query(image, question) -> str and detect(image, object) -> [box dict]."""
    app = FastAPI(title="Local Moondream", docs_url=None, redoc_url=None)

    # Plain `def` endpoints run in FastAPI's thread pool, so requests overlap up to the model's limit.
    @app.post("/v1/query")
    def query(req: QueryRequest) -> dict:
        return {"answer": model.query(decode_image(req.image_url), req.question)}

    @app.post("/v1/detect")
    def detect(req: DetectRequest) -> dict:
        return {"objects": model.detect(decode_image(req.image_url), req.object)}

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "backend": getattr(model, "backend", None), "model": getattr(model, "model_id", None)}

    return app


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--backend", choices=sorted(DEFAULT_MODELS), default="photon")
    p.add_argument("--model", help="model ID (default: moondream3.1-9B-A2B for photon, "
                                    "moondream/moondream3-preview for hf)")
    p.add_argument("--concurrency", type=int, default=1,
                   help="calls run on the GPU at once (default 1; try 4 if the backend batches them)")
    p.add_argument("--host", default="127.0.0.1", help="default 127.0.0.1: this machine only")
    p.add_argument("--port", type=int, default=2020)
    p.add_argument("-v", "--verbose", action="store_true", help="log each call's time")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    import uvicorn

    model = Model(args.backend, args.model, args.concurrency)
    uvicorn.run(create_app(model), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
