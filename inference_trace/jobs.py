"""Runs one video through the PPE pipeline with the recorder attached, one job at a time.

Uses the main app's backend as is: settings from .env, the Developer options saved in the
main app (or the defaults), its key handling, the same pipeline (ppe.video) and the same
summary (app.services.summary). Each trace lives in traces/<id>/: the input, the annotated
video, images/ and trace.json.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import cv2

from app.core.settings import Settings
from app.services.dev_settings import DevSettingsStore
from app.services.keys import endpoint_for, resolve_key
from app.services.summary import build_summary
from ppe.config import PPE_CATALOG, Config
from ppe.tuning import Tuning
from ppe.video import default_people_tracker, process_video

from .recorder import TraceRecorder, TracingAnalyzer

log = logging.getLogger(__name__)


def probe(path: Path) -> dict:
    cap = cv2.VideoCapture(str(path))
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps, n = cap.get(cv2.CAP_PROP_FPS) or 25.0, int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    if not w or not h:
        raise ValueError("Couldn't read this file as a video")
    return {"width": w, "height": h, "fps": round(fps, 3), "frames": n, "duration_sec": round(n / fps, 2)}


class TraceJobs:
    def __init__(self, folder: Path, settings: Settings, analyzer_factory=None, people_factory=None):
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self.settings = settings
        self.dev = DevSettingsStore(settings.data_dir)
        self.analyzer_factory = analyzer_factory or (lambda config, recorder: TracingAnalyzer(config, recorder))
        self.people_factory = people_factory  # tests: a stand-in for YOLO
        self._live: dict[str, dict] = {}  # id -> state of a queued/running trace
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="trace")

    # ---- reading -----------------------------------------------------------------
    def path(self, trace_id: str) -> Path:
        p = (self.folder / trace_id).resolve()
        if p.parent != self.folder.resolve() or not p.is_dir():
            raise KeyError(trace_id)
        return p

    def get(self, trace_id: str) -> dict:
        with self._lock:
            live = self._live.get(trace_id)
            if live is not None:
                rec: TraceRecorder | None = live.get("recorder")
                return {**live["meta"], "checks": list(rec.checks) if rec else [],
                        "tracks": sorted(rec.tracks.values(), key=lambda t: t["id"]) if rec else [],
                        "calls": dict(rec.calls) if rec else {}}
        return json.loads((self.path(trace_id) / "trace.json").read_text(encoding="utf-8"))

    def list(self) -> list[dict]:
        out = []
        for d in self.folder.iterdir():
            if not d.is_dir():
                continue
            try:
                t = self.get(d.name)
            except (KeyError, OSError, ValueError):
                continue
            out.append({k: t.get(k) for k in ("id", "name", "created_at", "status", "progress", "error")}
                       | {"violations": len((t.get("summary") or {}).get("events", []))})
        return sorted(out, key=lambda t: t["created_at"] or "", reverse=True)

    def delete(self, trace_id: str) -> None:
        with self._lock:
            if trace_id in self._live:
                raise RuntimeError("This trace is still running")
        shutil.rmtree(self.path(trace_id))

    # ---- running -------------------------------------------------------------------
    def start(self, src_name: str, write_input, *, required_ppe: list[str], interval: float,
              max_seconds: float | None, settings_choice: str, api_key: str | None) -> str:
        key, source = resolve_key(api_key, self.settings)  # raises MissingKey
        endpoint = endpoint_for(source, self.settings)
        tuning = self.dev.get() if settings_choice == "saved" else Tuning()
        trace_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        folder = self.folder / trace_id
        folder.mkdir()
        dest = folder / f"input{Path(src_name).suffix.lower() or '.mp4'}"
        try:
            write_input(dest)
            video = probe(dest)
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        step = max(1, round(interval * video["fps"]))
        meta = {
            "id": trace_id,
            "name": Path(src_name).stem,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "status": "queued",
            "error": None,
            "progress": {"done": 0, "total": video["frames"]},
            "video": video,
            "input": dest.name,
            "settings": {
                "choice": settings_choice,
                "required_ppe": required_ppe,
                "items": {i: PPE_CATALOG[i].label for i in required_ppe},
                "interval_sec": interval,
                "check_every_frames": step,
                "max_seconds": max_seconds,
                "key_source": source.value,
                "endpoint_host": urlsplit(endpoint).hostname,
                "model": tuning.moondream_model,
                "tuning": tuning.model_dump(),
                "questions": {i: tuning.question_for(i) for i in required_ppe},
                "verify_questions": {i: tuning.verify_question_for(i) for i in required_ppe},
                "multi_question": tuning.multi_question_for(required_ppe) if len(required_ppe) > 1 else None,
                "changes": tuning.changes(),
            },
        }
        config = Config(api_key=key, required_ppe=required_ppe, analysis_interval_sec=interval,
                        person_model=str(self.settings.person_model) if self.settings.person_model else None,
                        tuning=tuning, moondream_endpoint=endpoint)
        with self._lock:
            self._live[trace_id] = {"meta": meta}
        self._executor.submit(self._run, trace_id, folder, dest, config, max_seconds)
        return trace_id

    def _run(self, trace_id: str, folder: Path, src: Path, config: Config, max_seconds: float | None) -> None:
        with self._lock:
            live = self._live[trace_id]
            meta = live["meta"]
            recorder = live["recorder"] = TraceRecorder(folder, config.tuning.vote_window)
            meta["status"] = "running"
            meta["started_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

        def on_progress(done: int, total: int) -> None:
            meta["progress"] = {"done": done, "total": total or meta["progress"]["total"]}

        def on_start(width: int, height: int) -> None:
            meta["frame_size"] = [width, height]

        analyzer = self.analyzer_factory(config, recorder)
        trace = None
        try:
            report = process_video(
                src, folder / "annotated.mp4", analyzer, max_seconds=max_seconds,
                on_progress=on_progress, on_analysed=recorder.on_check, on_start=on_start,
                people_factory=lambda fps: recorder.tracker(
                    self.people_factory(fps) if self.people_factory else default_people_tracker(config, fps)))
            (folder / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
            summary = build_summary(report["timeline"], vote_window=config.tuning.vote_window,
                                    confirm_checks=config.tuning.confirm_checks)
            meta.update(status="completed", progress={"done": report["frames_written"], "total": report["frames_written"]},
                        finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        frames_written=report["frames_written"])
            trace = {**meta, "checks": recorder.checks,
                     "tracks": sorted(recorder.tracks.values(), key=lambda t: t["id"]),
                     "calls": recorder.calls, "summary": summary.model_dump(mode="json"),
                     "outputs": {"video": "annotated.mp4", "report": "report.json"}}
        except Exception as exc:
            log.exception("Trace %s failed", trace_id)
            meta.update(status="failed", error=str(exc) or exc.__class__.__name__)
            trace = {**meta, "checks": recorder.checks, "tracks": sorted(recorder.tracks.values(), key=lambda t: t["id"]),
                     "calls": recorder.calls, "summary": None, "outputs": {}}
        finally:
            analyzer.close()
            (folder / "trace.json").write_text(json.dumps(trace, indent=1), encoding="utf-8")
            with self._lock:
                self._live.pop(trace_id, None)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
