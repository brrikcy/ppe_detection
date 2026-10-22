"""Video pipeline: read -> analyse sampled frames -> annotate every frame -> write."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

import cv2
from PIL import Image

from .analyzer import PPEAnalyzer, WorkerResult
from .annotate import draw_results

log = logging.getLogger(__name__)


def process_video(
    input_path: str | Path,
    output_path: str | Path,
    analyzer: PPEAnalyzer,
    max_seconds: float | None = None,
) -> dict:
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

    step = max(1, int(round(analyzer.config.analysis_interval_sec * fps)))
    log.info("%s: %dx%d @ %.1ffps, %d frames, analysing every %d frames",
             input_path.name, width, height, fps, total, step)

    tmp_dir = Path(tempfile.mkdtemp())
    raw_path = tmp_dir / "raw.mp4"
    writer = cv2.VideoWriter(str(raw_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))

    current: list[WorkerResult] = []
    timeline = []
    idx = 0
    try:
        while True:
            if total > 0 and idx >= total:
                break
            ok, frame = cap.read()
            if not ok:
                break
            if idx % step == 0:
                rgb = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                try:
                    current = analyzer.analyze(rgb)
                except Exception:
                    log.exception("Analysis failed at frame %d; keeping previous results", idx)
                timeline.append({
                    "frame": idx,
                    "time_sec": round(idx / fps, 2),
                    "workers": [{"box": r.box, "label": r.label, "violations": r.violations}
                                for r in current],
                })
                n_bad = sum(not r.ok for r in current)
                log.info("frame %d (%.1fs): %d workers, %d with violations",
                         idx, idx / fps, len(current), n_bad)
            writer.write(draw_results(frame, current))
            idx += 1
    finally:
        cap.release()
        writer.release()

    _finalize(raw_path, input_path, output_path, max_seconds)
    shutil.rmtree(tmp_dir, ignore_errors=True)

    report = {
        "input": str(input_path),
        "output": str(output_path),
        "fps": fps,
        "frames_written": idx,
        "frames_analysed": len(timeline),
        "required_ppe": analyzer.config.required_ppe,
        "timeline": timeline,
    }
    report_path = output_path.with_suffix(".json")
    report_path.write_text(json.dumps(report, indent=2))
    log.info("Wrote %s and %s", output_path, report_path)
    return report


def _finalize(raw_path: Path, source: Path, output: Path, max_seconds: float | None):
    """Re-encode to browser-friendly H.264 and carry over the source audio if ffmpeg exists."""
    if not shutil.which("ffmpeg"):
        log.warning("ffmpeg not found; writing mp4v output (may not play in browsers)")
        shutil.move(raw_path, output)
        return
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw_path), "-i", str(source),
           "-map", "0:v:0", "-map", "1:a:0?", "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-shortest"]
    if max_seconds:
        cmd += ["-t", str(max_seconds)]
    cmd.append(str(output))
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError:
        log.warning("ffmpeg re-encode failed; writing mp4v output instead")
        shutil.move(raw_path, output)
