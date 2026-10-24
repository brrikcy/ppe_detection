"""Make sure annotated videos play in the browser, and recreate box-free frames.

Runs created on a machine without ffmpeg were saved as mp4v, which browsers
can't decode. The first time such a video is requested it is re-encoded to
H.264 in place; later requests are served directly.

Runs made before box-free copies of violation frames were saved get them from the
input video the first time the image viewer asks for one.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path

import cv2

from ppe.media import encode_for_browser, find_ffmpeg, is_browser_playable

log = logging.getLogger(__name__)


class VideoConverter:
    def __init__(self):
        self._guard = threading.Lock()
        self._locks: dict[Path, threading.Lock] = {}
        self._ok: dict[Path, float] = {}  # path -> mtime known to be playable

    def _lock_for(self, path: Path) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(path, threading.Lock())

    def warm(self, path: Path, audio_source: Path | None = None, max_seconds: float | None = None) -> None:
        """Convert in the background (if needed) so the video is ready when the player asks."""
        if path.exists() and self._ok.get(path) == path.stat().st_mtime:
            return
        threading.Thread(target=self.ensure_playable, args=(path, audio_source, max_seconds),
                         daemon=True, name="video-convert").start()

    def ensure_playable(self, path: Path, audio_source: Path | None = None,
                        max_seconds: float | None = None) -> bool:
        """Returns True if `path` is (now) browser-playable."""
        if not path.exists():
            return False
        with self._lock_for(path):  # the <video> tag fires several range requests at once
            mtime = path.stat().st_mtime
            if self._ok.get(path) == mtime:
                return True
            if not is_browser_playable(path):
                log.info("Re-encoding %s to H.264 for browser playback", path)
                if not encode_for_browser(path, path, audio_source=audio_source, max_seconds=max_seconds):
                    return False
                mtime = path.stat().st_mtime
            self._ok[path] = mtime
            return True


FRAME_MAX_WIDTH = 1280  # the size of the saved annotated frames (runner.PREVIEW_MAX_WIDTH)
_extract_lock = threading.Lock()


def extract_frame(src: Path, frame_index: int, fps: float, dest: Path, max_width: int = FRAME_MAX_WIDTH) -> bool:
    """Saves frame `frame_index` of the video `src`, without any drawing, as a JPEG at most
    `max_width` wide. Returns False if it couldn't be read."""
    # A quarter frame early, so rounding never lands on the next frame (ffmpeg takes the first at or after).
    t = max(0.0, (frame_index - 0.25) / fps) if fps > 0 else 0.0
    tmp = dest.with_name(f"{dest.stem}.tmp.jpg")
    with _extract_lock:  # the viewer also asks for neighbouring frames; one decode at a time
        if dest.exists():
            return True
        exe = find_ffmpeg()
        try:
            if exe:
                subprocess.run([exe, "-v", "error", "-y", "-ss", f"{t:.4f}", "-i", str(src), "-frames:v", "1",
                                "-vf", f"scale='min({max_width},iw)':-2", "-q:v", "3", str(tmp)],
                               capture_output=True, timeout=60, check=True)
            else:
                cap = cv2.VideoCapture(str(src))
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
                ok, frame = cap.read()
                cap.release()
                if not ok:
                    return False
                h, w = frame.shape[:2]
                if w > max_width:
                    frame = cv2.resize(frame, (max_width, int(h * max_width / w)), interpolation=cv2.INTER_AREA)
                cv2.imwrite(str(tmp), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if not tmp.exists():
                return False
            os.replace(tmp, dest)
            return True
        except (subprocess.SubprocessError, OSError):
            log.warning("Couldn't extract frame %d of %s", frame_index, src, exc_info=True)
            tmp.unlink(missing_ok=True)
            return False


_warming: set[Path] = set()


def warm_frames(jobs: list[tuple[Path, int, float, Path]]) -> None:
    """Extracts missing frames (src, index, fps, dest) in the background, one after another,
    so the image viewer finds them ready. Each dest is queued at most once at a time."""
    with _extract_lock:
        todo = [j for j in jobs if not j[3].exists() and j[3] not in _warming]
        _warming.update(j[3] for j in todo)
    if not todo:
        return

    def work():
        try:
            for src, index, fps, dest in todo:
                extract_frame(src, index, fps, dest)
        finally:
            with _extract_lock:
                _warming.difference_update(j[3] for j in todo)

    threading.Thread(target=work, daemon=True, name="frame-extract").start()
