"""Make sure annotated videos play in the browser.

Runs created on a machine without ffmpeg were saved as mp4v, which browsers
can't decode. The first time such a video is requested it is re-encoded to
H.264 in place; later requests are served directly.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from ppe.media import encode_for_browser, is_browser_playable

log = logging.getLogger(__name__)


class VideoConverter:
    def __init__(self):
        self._guard = threading.Lock()
        self._locks: dict[Path, threading.Lock] = {}
        self._ok: dict[Path, float] = {}  # path -> mtime known to be playable

    def _lock_for(self, path: Path) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(path, threading.Lock())

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
