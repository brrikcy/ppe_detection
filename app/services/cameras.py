"""Live camera wall: simulated RTSP cameras, each a video played in a continuous loop.

Every camera is a thread that plays its video at the video's own speed (like a camera feed),
tracks the people in it, checks their PPE and draws the result, with ppe.video.process_video.
When the video ends it starts again, so the "stream" never ends. Nothing is recorded: frames go
straight to the live feeds (JPEG over WebSocket), and each loop's violation incidents are kept
with a photo, so they show up in the dashboard's violation list as they are found.

A camera is "on the dashboard" from start() until stop(); its violations stay in the list.
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import cv2

from ppe.analyzer import PPEAnalyzer, WorkerResult
from ppe.config import Config
from ppe.moondream_client import AuthError, ModelError
from ppe.tuning import Tuning
from ppe.video import Stopped, process_video

from .runner import LiveFeed, RunRunner
from .summary import build_summary, clean_frame_name, frame_name, snapshot_name

log = logging.getLogger(__name__)

VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}
PROCESS_HEIGHT = 720  # cameras are processed at this height: 11 feeds at 1080p don't fit a CPU
CAMERA_FPS = 8.0  # frames a second each camera is processed at (CCTV analytics rarely needs more)
DETECT_FPS = 3.0  # person detections a second, per camera (the tracker moves boxes in between)
MAX_PARALLEL_REQUESTS = 3  # Moondream requests in flight, per camera
START_STAGGER_SEC = 1.0  # cameras started together come up one after the other
RETRY_SEC = 10  # after a failed loop (e.g. Moondream unreachable), try again after this long
MAX_EVENTS_PER_CAMERA = 150  # violations kept per camera; older ones are dropped with their photos
THUMB = {"max_width": 480, "quality": 55, "fps": 8}  # tiles on the wall
FULL = {"max_width": 1280, "quality": 70, "fps": 25}  # one camera enlarged
SNAPSHOT_MAX_HEIGHT = 320


def camera_id(path: Path) -> str:
    """File name without the extension and without a site suffix such as "-w1"."""
    return re.sub(r"-w\d+$", "", path.stem).lower()


def camera_name(cid: str) -> str:
    return cid.replace("_", " ").title()


@dataclass
class Session:
    """One camera while it is on the dashboard."""

    id: str
    name: str
    path: Path
    duration_sec: float
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    state: str = "starting"  # starting, live, retrying
    error: str | None = None
    loop: int = 0
    started_at: float = field(default_factory=time.time)
    frame_size: list[int] | None = None
    checking: int = 0
    stats: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)  # incidents of the loop in progress
    frames_in_loop: int = 0
    feeds: dict[str, LiveFeed] = field(default_factory=dict)

    def __post_init__(self):
        self.feeds = {"thumb": LiveFeed(**THUMB), "full": LiveFeed(**FULL)}


@dataclass
class StartOptions:
    api_key: str
    endpoint: str
    required_ppe: list[str]
    analysis_interval_sec: float
    tuning: Tuning


class CameraManager:
    def __init__(self, data_dir: Path, video_dir: Path, analyzer_factory: Callable[[Config], PPEAnalyzer],
                 people_factory=None, person_model: Path | None = None,
                 fps: float = CAMERA_FPS, detect_fps: float = DETECT_FPS):
        self.video_dir = Path(video_dir)
        self.fps, self.detect_fps = fps, detect_fps
        self.root = Path(data_dir) / "_cameras"
        self.analyzer_factory = analyzer_factory
        self.people_factory = people_factory
        self.person_model = person_model
        self._lock = threading.RLock()
        self._active: dict[str, Session] = {}
        self._history: dict[str, list[dict]] = {}  # finished loops' incidents, per camera
        self._probe_cache: dict[tuple[str, float], tuple[float, float]] = {}  # (duration s, source fps)
        self._seq = 0
        # Photos are only meaningful for this server process: start from a clean folder.
        shutil.rmtree(self.root, ignore_errors=True)

    # -- catalog ---------------------------------------------------------------
    def _probe(self, path: Path) -> tuple[float, float]:
        key = (str(path), path.stat().st_mtime)
        if key not in self._probe_cache:
            cap = cv2.VideoCapture(str(path))
            try:
                fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
                self._probe_cache[key] = (round(cap.get(cv2.CAP_PROP_FRAME_COUNT) / fps, 1), fps)
            finally:
                cap.release()
        return self._probe_cache[key]

    def _duration(self, path: Path) -> float:
        return self._probe(path)[0]

    def _videos(self) -> dict[str, Path]:
        if not self.video_dir.is_dir():
            return {}
        found = {camera_id(p): p for p in sorted(self.video_dir.iterdir())
                 if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES}
        return dict(sorted(found.items()))

    def has(self, cid: str) -> bool:
        return cid in self._videos()

    def state(self) -> dict:
        """Everything the dashboard shows: every camera that has a video, and how it is doing."""
        with self._lock:
            active = dict(self._active)
            cameras = []
            for cid, path in self._videos().items():
                s = active.get(cid)
                cam = {"id": cid, "name": camera_name(cid), "active": s is not None,
                       "duration_sec": self._duration(path),
                       "stream": f"rtsp://simulated/{cid}"}
                if s is not None:
                    cam.update(state=s.state, error=s.error, loop=s.loop, checking=s.checking,
                               frame_size=s.frame_size, uptime_sec=round(time.time() - s.started_at),
                               stats=s.stats)
                cameras.append(cam)
            events = self._events()
        live = [c for c in cameras if c["active"]]
        return {
            "cameras": cameras,
            "totals": {
                "live": len(live),
                "workers_now": sum(c["stats"].get("workers_now", 0) for c in live if c.get("stats")),
                "violating_now": sum(c["stats"].get("violating_now", 0) for c in live if c.get("stats")),
                "violations": len(events),
            },
            "violations": events[:300],
        }

    # -- starting and stopping --------------------------------------------------------
    def start(self, ids: list[str], options: StartOptions) -> list[str]:
        """Puts cameras on the dashboard and starts their streams; returns the ids started now."""
        videos = self._videos()
        started = []
        for cid in ids:
            if cid not in videos:
                raise KeyError(cid)
        for cid in ids:
            with self._lock:
                if cid in self._active:
                    continue
                s = Session(id=cid, name=camera_name(cid), path=videos[cid],
                            duration_sec=self._duration(videos[cid]))
                self._active[cid] = s
            delay = len(started) * START_STAGGER_SEC
            s.thread = threading.Thread(target=self._run, args=(s, options, delay), name=f"camera-{cid}",
                                        daemon=True)
            s.thread.start()
            started.append(cid)
        return started

    def stop(self, cid: str) -> bool:
        """Takes a camera off the dashboard. Its violations stay in the list."""
        with self._lock:
            s = self._active.pop(cid, None)
        if s is None:
            return False
        s.stop.set()
        for feed in s.feeds.values():
            feed.close()  # viewers' streams end
        return True

    def stop_all(self) -> None:
        for cid in list(self._active):
            self.stop(cid)

    def shutdown(self) -> None:
        threads = []
        with self._lock:
            threads = [s.thread for s in self._active.values() if s.thread]
        self.stop_all()
        for t in threads:
            t.join(timeout=3)

    def clear_violations(self) -> None:
        with self._lock:
            for events in self._history.values():
                self._drop_files(events)
            self._history.clear()
            for s in self._active.values():
                self._drop_files(s.events)
                s.events = []

    def feed(self, cid: str, size: str) -> LiveFeed | None:
        with self._lock:
            s = self._active.get(cid)
            return s.feeds.get("full" if size == "full" else "thumb") if s else None

    def image(self, cid: str, token: str, folder: str, name: str) -> Path | None:
        if folder not in ("snapshots", "frames") or Path(name).name != name or not name.endswith(".jpg"):
            return None
        if not re.fullmatch(r"p\d+", token) or not re.fullmatch(r"[a-z0-9_]+", cid):
            return None
        path = self.root / cid / token / folder / name
        return path if path.exists() else None

    # -- violations --------------------------------------------------------------
    def _events(self) -> list[dict]:
        with self._lock:
            events = [e for ev in self._history.values() for e in ev]
            events += [e for s in self._active.values() for e in s.events]
        return sorted(events, key=lambda e: e["detected_at"], reverse=True)

    def _drop_files(self, events: list[dict]) -> None:
        for e in events:
            for key in ("snapshot", "frame", "clean"):
                if e.get(key):
                    (self.root / e["camera_id"] / e["token"] / e[key + "_dir"] / e[key]).unlink(missing_ok=True)

    def _finish_loop(self, s: Session, token: str, events: list[dict]) -> None:
        """Keeps the loop's incidents with their photos; the rest of the loop's images go."""
        with self._lock:
            s.events = []
            history = self._history.setdefault(s.id, [])
            history.extend(events)
            if len(history) > MAX_EVENTS_PER_CAMERA:
                drop = history[:len(history) - MAX_EVENTS_PER_CAMERA]
                del history[:len(drop)]
                self._drop_files(drop)
        keep = {(e[k + "_dir"], e[k]) for e in events for k in ("snapshot", "frame", "clean") if e.get(k)}
        pdir = self.root / s.id / token
        for folder in ("snapshots", "frames"):
            for f in (pdir / folder).glob("*.jpg") if (pdir / folder).exists() else []:
                if (folder, f.name) not in keep:
                    f.unlink(missing_ok=True)

    # -- the camera's thread ---------------------------------------------------------
    def _run(self, s: Session, opt: StartOptions, delay: float) -> None:
        if s.stop.wait(delay):
            return
        tun = opt.tuning.model_copy(update={
            "detect_fps": min(opt.tuning.detect_fps or self.detect_fps, self.detect_fps),
            "parallel_requests": min(opt.tuning.parallel_requests, MAX_PARALLEL_REQUESTS),
            "fixed_camera": True,  # CCTV: no camera motion to compensate for
        })
        config = Config(api_key=opt.api_key, required_ppe=opt.required_ppe,
                        analysis_interval_sec=opt.analysis_interval_sec,
                        person_model=str(self.person_model) if self.person_model else None,
                        tuning=tun, moondream_endpoint=opt.endpoint)
        analyzer = self.analyzer_factory(config)
        try:
            while not s.stop.is_set():
                s.loop += 1
                try:
                    self._play_loop(s, analyzer)
                    s.error = None
                except Stopped:
                    break
                except (AuthError, ModelError) as exc:
                    s.state, s.error = "retrying", ("The API key was rejected." if isinstance(exc, AuthError)
                                                    else f"The vision model doesn't accept {tun.moondream_model!r}.")
                    log.error("Camera %s stopped: %s", s.id, s.error)
                    return
                except Exception as exc:
                    log.exception("Camera %s: loop %d failed", s.id, s.loop)
                    s.state, s.error = "retrying", str(exc) or exc.__class__.__name__
                    if s.stop.wait(RETRY_SEC):
                        break
        finally:
            analyzer.close()
            with self._lock:
                if self._active.get(s.id) is s:
                    s.state = "retrying" if s.error else "stopped"

    def _play_loop(self, s: Session, analyzer: PPEAnalyzer) -> None:
        with self._lock:
            self._seq += 1
            token = f"p{self._seq}"
        pdir = self.root / s.id / token
        (pdir / "snapshots").mkdir(parents=True, exist_ok=True)
        (pdir / "frames").mkdir(exist_ok=True)
        tun = analyzer.config.tuning
        every = max(1, round(self._probe(s.path)[1] / self.fps))  # process every n-th frame
        timeline: list[dict] = []
        s.frames_in_loop = 0

        def names(folder: str) -> set[str]:
            return {p.name for p in (pdir / folder).glob("*.jpg")}

        def events_now() -> list[dict]:
            summary = build_summary(timeline, names("snapshots"), names("frames"), set(),
                                    tun.vote_window, tun.confirm_checks)
            s.stats = _stats(summary)
            return [_event(s, token, e) for e in summary.events]

        def on_start(w: int, h: int) -> None:
            s.frame_size = [w, h]

        def on_frame(idx: int, annotated) -> None:
            s.frames_in_loop = idx
            if s.state != "live":
                s.state = "live"
            for feed in s.feeds.values():
                if feed.viewers:
                    feed.push(annotated)

        def on_check(idx: int, workers: int) -> None:
            s.checking = workers

        def on_analysed(entry: dict, frame, results: list[WorkerResult], display: list[WorkerResult]) -> None:
            try:
                _save_violation_crops(pdir / "snapshots", entry["frame"], frame, results)
                RunRunner._save_preview(pdir / "preview.jpg", frame, display,
                                        violation_copy=pdir / "frames" / frame_name(entry["frame"]),
                                        keep_copy=any(r.violations for r in display), update_preview=False)
            except OSError:
                log.warning("Camera %s: couldn't save images for frame %d", s.id, entry["frame"], exc_info=True)
            timeline.append(entry)
            events = events_now()
            with self._lock:
                if self._active.get(s.id) is s:
                    s.events = events

        try:
            process_video(s.path, pdir / "unused.mp4", analyzer, on_analysed=on_analysed, on_frame=on_frame,
                          on_check=on_check, on_start=on_start, people_factory=self.people_factory,
                          live=True, record=False, stop=s.stop, max_height=PROCESS_HEIGHT,
                          sample_every=every)
        finally:
            s.checking = 0
            # Incidents of this loop, also when it was stopped half-way.
            self._finish_loop(s, token, events_now() if timeline else [])


def _save_violation_crops(folder: Path, frame_idx: int, frame, results: list[WorkerResult]) -> None:
    """Crops of the workers found missing PPE in this check (the photo of an incident)."""
    h, w = frame.shape[:2]
    for i, r in enumerate(results):
        if not r.violations or r.too_small or r.pending or r.check_failed:
            continue
        x1, y1, x2, y2 = r.box
        pad_x, pad_y = int((x2 - x1) * 0.1), int((y2 - y1) * 0.1)
        crop = frame[max(0, y1 - pad_y):min(h, y2 + pad_y), max(0, x1 - pad_x):min(w, x2 + pad_x)]
        if crop.size == 0:
            continue
        ch, cw = crop.shape[:2]
        if ch > SNAPSHOT_MAX_HEIGHT:
            crop = cv2.resize(crop, (max(1, int(cw * SNAPSHOT_MAX_HEIGHT / ch)), SNAPSHOT_MAX_HEIGHT))
        cv2.imwrite(str(folder / snapshot_name(frame_idx, i)), crop, [cv2.IMWRITE_JPEG_QUALITY, 85])


def _stats(summary) -> dict:
    now = summary.timeline[-1] if summary.timeline else None
    return {"workers_now": now.workers if now else 0, "violating_now": now.violations if now else 0,
            "compliance": summary.compliance_rate, "checks": summary.worker_checks,
            "people": len(summary.people)}


def _event(s: Session, token: str, e) -> dict:
    base = f"/api/cameras/{s.id}/images/{token}"
    ev = {
        "id": f"{s.id}-{token}-{e.track_id}-{e.frame}",
        "camera_id": s.id, "camera_name": s.name, "token": token, "loop": s.loop,
        "track_id": e.track_id, "time_sec": e.time_sec, "end_sec": e.end_sec, "checks": e.checks,
        "violations": e.violations, "box": e.box, "frame_size": s.frame_size, "resolved": e.resolved,
        "detected_at": (e.detected_at or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "snapshot": e.snapshot, "snapshot_dir": "snapshots",
        "frame": e.frame_image, "frame_dir": "frames",
        "clean": clean_frame_name(e.frame_image) if e.frame_image else None, "clean_dir": "frames",
    }
    ev["crop_url"] = f"{base}/snapshots/{e.snapshot}" if e.snapshot else None
    ev["frame_url"] = f"{base}/frames/{e.frame_image}" if e.frame_image else None
    ev["clean_url"] = f"{base}/frames/{ev['clean']}" if ev["clean"] else None
    return ev
