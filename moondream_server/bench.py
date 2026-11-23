"""Time real PPE checks against a Moondream endpoint, to compare the local server with the Cloud.

    python -m moondream_server.bench videos/site.mp4                                  # MOONDREAM_ENDPOINT
    python -m moondream_server.bench videos/site.mp4 --endpoint http://127.0.0.1:2020/v1

Takes one frame per second from the video (at most 1080p, as in live mode), finds the workers
with YOLO, and runs the same check as a run (current developer settings). Prints the time of
each check and each Moondream call, and what each worker was found to wear, so two endpoints
can be compared on speed and answers.
"""

from __future__ import annotations

import argparse
import logging
import statistics
import threading
import time
from pathlib import Path

import cv2
from PIL import Image

from ppe.analyzer import PPEAnalyzer
from ppe.config import Config
from ppe.media import FrameReader
from ppe.moondream_client import MoondreamCloud
from ppe.people import DEFAULT_MODEL, PERSON_CLASS, _load_model
from ppe.tuning import Tuning
from ppe.video import LIVE_MAX_HEIGHT


class Timed:
    """Wraps the Moondream client and records each call's time."""

    def __init__(self, model):
        self.model, self.times, self._lock = model, {"query": [], "detect": []}, threading.Lock()

    def _timed(self, name, *args):
        t = time.perf_counter()
        try:
            return getattr(self.model, name)(*args)
        finally:
            with self._lock:
                self.times[name].append(time.perf_counter() - t)

    def query(self, image, question):
        return self._timed("query", image, question)

    def detect(self, image, obj):
        return self._timed("detect", image, obj)


def _ms(values: list[float]) -> str:
    if not values:
        return "none"
    values = sorted(values)
    p95 = values[min(len(values) - 1, int(round(0.95 * (len(values) - 1))))]
    return f"{len(values)} calls, median {statistics.median(values) * 1000:.0f} ms, p95 {p95 * 1000:.0f} ms"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("video")
    p.add_argument("--endpoint", help="Moondream API base URL (default: MOONDREAM_ENDPOINT or the Cloud)")
    p.add_argument("--frames", type=int, default=8, help="frames to check, one per second (default 8)")
    p.add_argument("--tuning", help="JSON file of developer settings (default: runs/_dev/tuning.json if saved)")
    args = p.parse_args()
    logging.basicConfig(level=logging.WARNING)

    tuning_file = Path(args.tuning or "runs/_dev/tuning.json")
    tuning = Tuning.model_validate_json(tuning_file.read_text()) if tuning_file.exists() else Tuning()
    cfg = Config.from_env(tuning=tuning)
    if args.endpoint:
        cfg.moondream_endpoint = args.endpoint.rstrip("/")
    client = Timed(MoondreamCloud(cfg.api_key, endpoint=cfg.moondream_endpoint, model=tuning.moondream_model))
    analyzer = PPEAnalyzer(cfg, model=client)
    detector = _load_model(Path(DEFAULT_MODEL))
    print(f"Endpoint {cfg.moondream_endpoint}, items {', '.join(cfg.required_ppe)}, "
          f"{tuning.parallel_requests} parallel requests")

    reader = FrameReader(Path(args.video), max_height=LIVE_MAX_HEIGHT)
    step = max(1, int(round(reader.fps)))
    check_times, idx, done = [], 0, 0
    try:
        while done < args.frames and (frame := reader.read()) is not None:
            idx += 1
            if (idx - 1) % step:
                continue
            boxes = [tuple(int(v) for v in b) for b in detector.predict(
                frame, classes=[PERSON_CLASS], conf=0.4, verbose=False)[0].boxes.xyxy.tolist()]
            rgb = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            t = time.perf_counter()
            results = analyzer.check_workers(rgb, boxes)
            dt = time.perf_counter() - t
            checked = [r for r in results if not r.too_small]
            if checked:
                check_times.append(dt)
            print(f"{(idx - 1) / reader.fps:5.1f}s  {len(checked)} workers checked in {dt * 1000:5.0f} ms:  "
                  + ("; ".join(r.label for r in checked) or "-"))
            done += 1
    finally:
        reader.close()
        analyzer.close()

    print(f"\nWhole checks: {_ms(check_times)}")
    print(f"query:  {_ms(client.times['query'])}")
    print(f"detect: {_ms(client.times['detect'])}")


if __name__ == "__main__":
    main()
