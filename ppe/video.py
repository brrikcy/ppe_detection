"""Video pipeline: track people on every frame -> check PPE on a schedule -> annotate -> encode."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

import cv2
from PIL import Image

from .analyzer import PPEAnalyzer, WorkerResult
from .annotate import draw_results
from .media import FrameWriter
from .moondream_client import AuthError
from .status import PersonStatus

log = logging.getLogger(__name__)


def process_video(
    input_path: str | Path,
    output_path: str | Path,
    analyzer: PPEAnalyzer,
    max_seconds: float | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    on_analysed: Callable[[dict, "cv2.typing.MatLike", list[WorkerResult]], None] | None = None,
    people_factory: Callable[[float], object] | None = None,
) -> dict:
    """Annotate `input_path` into `output_path` and return the report dict.

    Every frame, the person tracker made by `people_factory(fps)` (default: YOLO11n +
    BoT-SORT, see ppe.people) gives the tracked workers. Every analysis interval, each worker big enough to judge gets a PPE check;
    a worker who appears between checks is checked straight away. The video shows each
    worker's status voted over their recent checks (ppe.status).

    on_progress(frames_done, total_frames) is called periodically while frames are written.
    on_analysed(timeline_entry, frame_bgr, results) is called after each check frame.
    """
    input_path, output_path = Path(input_path), Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cap = cv2.VideoCapture(str(input_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {input_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if max_seconds:
        total = min(total, int(max_seconds * fps)) if total > 0 else int(max_seconds * fps)

    cfg = analyzer.config
    step = max(1, int(round(cfg.analysis_interval_sec * fps)))
    min_check_h = cfg.min_check_height_frac * height
    log.info("%s: %dx%d @ %.1ffps, %d frames, checking every %d frames",
             input_path.name, width, height, fps, total, step)

    if people_factory is None:
        from .people import DEFAULT_MODEL, YoloPersonTracker

        people = YoloPersonTracker(fps=fps, model_path=Path(cfg.person_model or DEFAULT_MODEL),
                                   imgsz=cfg.person_imgsz)
    else:
        people = people_factory(fps)
    writer = FrameWriter(output_path, width, height, fps, audio_source=input_path, max_seconds=max_seconds)

    statuses: dict[int, PersonStatus] = {}
    timeline = []
    tried = failures = 0
    idx = 0
    try:
        while True:
            if total > 0 and idx >= total:
                break
            ok, frame = cap.read()
            if not ok:
                break
            tracked = people.update(frame)
            for p in tracked:
                statuses.setdefault(p.id, PersonStatus())
            checkable = [p for p in tracked if p.box[3] - p.box[1] >= min_check_h]
            is_check = idx % step == 0
            if is_check:
                due = checkable
            elif step - idx % step > step // 2:
                # Someone new is checked now rather than at the next check frame, unless that's close.
                due = [p for p in checkable if not statuses[p.id].checks]
            else:
                due = []

            results: dict[int, WorkerResult] = {}
            failed = False
            if due:
                tried += 1
                rgb = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                try:
                    checked = analyzer.check_workers(rgb, [p.box for p in due], [p.box for p in tracked])
                except AuthError:
                    raise
                except Exception:
                    failures += 1
                    failed = True
                    log.exception("PPE check failed at frame %d; skipping it", idx)
                else:
                    for p, r in zip(due, checked):
                        r.track_id = p.id
                        results[p.id] = r
                        statuses[p.id].add(r, idx)

            if is_check and not failed:
                current = [results.get(p.id) or WorkerResult(box=p.box, track_id=p.id, too_small=True)
                           for p in tracked]
                entry = {
                    "frame": idx,
                    "time_sec": round(idx / fps, 2),
                    "workers": [{"track_id": r.track_id, "box": list(r.box), "label": r.label,
                                 "violations": r.violations, "unverified": r.unverified,
                                 "too_small": r.too_small}
                                for r in current],
                }
                timeline.append(entry)
                if on_analysed:
                    on_analysed(entry, frame, current)
                log.info("frame %d (%.1fs): %d workers, %d with violations",
                         idx, idx / fps, len(current), sum(not r.ok for r in current))

            display = [statuses[p.id].display(p.id, p.box, too_small=p.box[3] - p.box[1] < min_check_h)
                       for p in tracked]
            writer.write(draw_results(frame, display))
            idx += 1
            if on_progress and (idx % 10 == 0 or idx == total):
                on_progress(idx, total)
        if tried and failures == tried:
            raise RuntimeError("Moondream analysis failed on every check; see the server log")
        writer.close()
    except BaseException:
        writer.abort()
        raise
    finally:
        cap.release()

    report = {
        "input": str(input_path),
        "output": str(output_path),
        "fps": fps,
        "width": width,
        "height": height,
        "duration_sec": round(idx / fps, 2),
        "analysis_interval_sec": cfg.analysis_interval_sec,
        "frames_written": idx,
        "frames_analysed": len(timeline),
        "required_ppe": cfg.required_ppe,
        "timeline": timeline,
    }
    report_path = output_path.with_suffix(".json")
    report_path.write_text(json.dumps(report, indent=2))
    log.info("Wrote %s and %s", output_path, report_path)
    return report
