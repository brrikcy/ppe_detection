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


class FrameReader:
    """Reads the BGR frames of `src` in order.

    Decoding goes through ffmpeg when it's available: a separate, multi-threaded process
    that is several times faster than OpenCV for HD video and runs alongside person
    detection. Its first frame is compared with OpenCV's, and OpenCV decodes instead if they
    differ (e.g. a rotation flag one of them ignores) or ffmpeg can't read the file.
    """

    MAX_MEAN_DIFF = 8.0  # per pixel, 0-255; compression differences between decoders are ~1

    def __init__(self, src: Path):
        self.src = str(src)
        cap = cv2.VideoCapture(self.src)
        if not cap.isOpened():
            raise RuntimeError(f"Cannot open video: {src}")
        self.fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        self.frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        ok, first = cap.read()
        cap.release()
        if not ok:
            raise RuntimeError(f"No frames could be decoded from {src}")
        self.height, self.width = first.shape[:2]
        self._frame_bytes = self.width * self.height * 3
        self._proc = self._cap = None
        self._pending = None  # first ffmpeg frame, already read for the comparison
        ffmpeg = find_ffmpeg()
        if ffmpeg:
            self._proc = subprocess.Popen(
                [ffmpeg, "-loglevel", "error", "-nostdin", "-threads", "0", "-i", self.src, "-map", "0:v:0",
                 "-vsync", "0", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=self._frame_bytes * 2)
            frame = self._read_ffmpeg()
            if frame is None or float(cv2.absdiff(frame, first).mean()) > self.MAX_MEAN_DIFF:
                log.info("ffmpeg frames don't match OpenCV's for %s; decoding with OpenCV", src)
                self._stop_ffmpeg()
            else:
                self._pending = frame
        if self._proc is None:
            self._cap = cv2.VideoCapture(self.src)
        self.decoder = "ffmpeg" if self._proc else "opencv"

    def _read_ffmpeg(self):
        import numpy as np

        buf = self._proc.stdout.read(self._frame_bytes)
        if len(buf) < self._frame_bytes:
            return None
        return np.frombuffer(buf, np.uint8).reshape(self.height, self.width, 3).copy()

    def read(self):
        """The next frame, or None at the end of the video."""
        if self._cap is not None:
            ok, frame = self._cap.read()
            return frame if ok else None
        if self._pending is not None:
            frame, self._pending = self._pending, None
            return frame
        return self._read_ffmpeg()

    def _stop_ffmpeg(self) -> None:
        if self._proc is not None:
            if self._proc.poll() is None:
                self._proc.kill()
            self._proc.stdout.close()
            self._proc.wait()
            self._proc = None

    def close(self) -> None:
        self._stop_ffmpeg()
        if self._cap is not None:
            self._cap.release()
            self._cap = None
