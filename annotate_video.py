"""CLI: annotate a construction-site video with per-worker PPE compliance tags.

Usage:
    python annotate_video.py input.mp4 [-o outputs/input_annotated.mp4]
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from ppe.analyzer import PPEAnalyzer
from ppe.config import Config
from ppe.video import process_video


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", help="Path to input video")
    p.add_argument("-o", "--output", help="Output video path (default: outputs/<name>_annotated.mp4)")
    p.add_argument("--interval", type=float, help="Seconds between analysed frames (default 1.0)")
    p.add_argument("--required", help="Comma separated PPE items, e.g. helmet,vest,gloves")
    p.add_argument("--max-seconds", type=float, help="Only process the first N seconds")
    p.add_argument("--workers", type=int, help="Parallel Moondream requests")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    cfg = Config.from_env(analysis_interval_sec=args.interval, required_ppe=args.required,
                          max_workers=args.workers)
    inp = Path(args.input)
    out = Path(args.output) if args.output else Path("outputs") / f"{inp.stem}_annotated.mp4"

    analyzer = PPEAnalyzer(cfg)
    try:
        report = process_video(inp, out, analyzer, max_seconds=args.max_seconds)
    finally:
        analyzer.close()
    print(f"\nAnnotated video: {out}\nReport: {out.with_suffix('.json')}"
          f"\nFrames analysed: {report['frames_analysed']}")


if __name__ == "__main__":
    main()
