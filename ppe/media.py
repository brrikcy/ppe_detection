"""Browser-playable video output (H.264 + AAC in MP4).

OpenCV can only write MPEG-4 Part 2 ("mp4v") portably, which browsers won't
play, so annotated frames are piped into ffmpeg (FrameWriter), and older mp4v
runs are re-encoded (encode_for_browser). A system ffmpeg is used if present;
otherwise the binary bundled with the `imageio-ffmpeg` package.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
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
    cmd += _output_args(audio_source, max_seconds) + [str(tmp)]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        stderr = getattr(exc, "stderr", b"") or b""
        log.warning("ffmpeg encode failed: %s %s", exc, stderr.decode(errors="replace")[-500:])
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(dst)
    return True


def _output_args(audio_source: Path | None, max_seconds: float | None) -> list[str]:
    """ffmpeg arguments after the video input (input 0): optional audio, H.264, MP4."""
    args = []
    if audio_source:
        args += ["-i", str(audio_source), "-map", "0:v:0", "-map", "1:a:0?", "-c:a", "aac", "-shortest"]
    args += ["-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",  # yuv420p needs even dimensions
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "20",
             "-movflags", "+faststart"]  # moov atom first so playback starts before the download ends
    if max_seconds:
        args += ["-t", str(max_seconds)]
    return args


class FrameWriter:
    """Writes BGR frames to a browser-playable MP4 at `dst`.

    Frames are piped straight into ffmpeg, so the video is compressed once. Without
    ffmpeg, OpenCV writes mp4v instead (plays in desktop players, not browsers).
    The file only appears at `dst` once close() succeeds.
    """

    def __init__(self, dst: Path, width: int, height: int, fps: float,
                 audio_source: Path | None = None, max_seconds: float | None = None):
        self.dst = Path(dst)
        self._tmp = self.dst.with_name(self.dst.stem + ".encoding.mp4")
        self._proc = self._cv = None
        ffmpeg = find_ffmpeg()
        if ffmpeg:
            cmd = [ffmpeg, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
                   "-s", f"{width}x{height}", "-r", f"{fps:.6f}", "-i", "pipe:0"]
            cmd += _output_args(audio_source, max_seconds) + [str(self._tmp)]
            self._stderr = tempfile.TemporaryFile()
            self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                          stderr=self._stderr)
        else:
            log.warning("No ffmpeg found (install it, or `pip install imageio-ffmpeg`); "
                        "output video may not play in browsers")
            self._cv = cv2.VideoWriter(str(self._tmp), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))

    def write(self, frame) -> None:
        if self._cv is not None:
            self._cv.write(frame)
            return
        try:
            self._proc.stdin.write(frame.tobytes())
        except (BrokenPipeError, OSError):
            raise RuntimeError(f"ffmpeg stopped while encoding: {self._error()}") from None

    def close(self) -> None:
        if self._cv is not None:
            self._cv.release()
        else:
            self._proc.stdin.close()
            if self._proc.wait() != 0:
                raise RuntimeError(f"ffmpeg encode failed: {self._error()}")
        self._tmp.replace(self.dst)

    def abort(self) -> None:
        if self._cv is not None:
            self._cv.release()
        elif self._proc.poll() is None:
            self._proc.kill()
            self._proc.wait()
        self._tmp.unlink(missing_ok=True)

    def _error(self) -> str:
        self._stderr.seek(0)
        return self._stderr.read().decode(errors="replace")[-500:].strip()
