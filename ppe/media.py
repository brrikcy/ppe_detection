"""Browser-playable video output (H.264 + AAC in MP4).

OpenCV can only write MPEG-4 Part 2 ("mp4v") portably, which browsers won't
play, so the final file is re-encoded with ffmpeg. A system ffmpeg is used if
present; otherwise the binary bundled with the `imageio-ffmpeg` package.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

import cv2

log = logging.getLogger(__name__)

BROWSER_FOURCCS = {"avc1", "h264", "H264", "AVC1"}


def find_ffmpeg() -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def video_fourcc(path: Path) -> str:
    cap = cv2.VideoCapture(str(path))
    try:
        code = int(cap.get(cv2.CAP_PROP_FOURCC))
    finally:
        cap.release()
    return "".join(chr((code >> 8 * i) & 0xFF) for i in range(4)).strip("\x00 ")


def is_browser_playable(path: Path) -> bool:
    return video_fourcc(path) in BROWSER_FOURCCS


def encode_for_browser(src: Path, dst: Path, audio_source: Path | None = None,
                       max_seconds: float | None = None) -> bool:
    """Encode `src` to H.264 MP4 at `dst`, muxing audio from `audio_source` if it has any.

    Returns False (leaving dst untouched) if no ffmpeg is available or encoding fails.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        log.warning("No ffmpeg found (install it, or `pip install imageio-ffmpeg`); "
                    "output video may not play in browsers")
        return False
    tmp = dst.with_name(dst.stem + ".encoding.mp4")
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(src)]
    if audio_source:
        cmd += ["-i", str(audio_source), "-map", "0:v:0", "-map", "1:a:0?", "-c:a", "aac", "-shortest"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "23",
            "-movflags", "+faststart"]  # moov atom first so playback starts before the download ends
    if max_seconds:
        cmd += ["-t", str(max_seconds)]
    cmd.append(str(tmp))
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        stderr = getattr(exc, "stderr", b"") or b""
        log.warning("ffmpeg encode failed: %s %s", exc, stderr.decode(errors="replace")[-500:])
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(dst)
    return True
