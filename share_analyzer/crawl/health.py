"""HealthMonitor — detects share disconnects mid-crawl.

If the SMB mount drops, every subsequent `os.scandir` raises an OSError;
the walker dutifully records each as a WalkError and the run "succeeds"
with thousands of bogus entries and zero files. This monitor watches
the error stream, and when a sustained burst of network-class errors
coincides with the root path being unreachable, it tells the
orchestrator to abort and mark the run `status='disconnected'`.

The decision is intentionally two-stage:
  1. Cheap, in-memory check — count network-class errors in a sliding
     time window.
  2. Confirm by stat-ing the root once (single syscall). Localised
     network blips don't mark the share as down.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Deque, Optional


_NETWORK_HINTS = (
    "ETIMEDOUT", "ECONNRESET", "EHOSTUNREACH", "ENETUNREACH",
    "ENETDOWN", "ENETRESET", "EPIPE", "EIO",
    "TimeoutError", "ConnectionError", "ConnectionAbortedError",
    "ConnectionResetError",
    # Windows winerror tokens commonly surface by name in str(exc).
    "ERROR_NETNAME_DELETED", "ERROR_BAD_NET_NAME", "ERROR_BAD_NETPATH",
    "WinError 53", "WinError 58", "WinError 59", "WinError 64",
    "WinError 67", "WinError 121", "WinError 1231", "WinError 1232",
)


def _looks_like_network_error(reason: str) -> bool:
    return any(hint in reason for hint in _NETWORK_HINTS)


class HealthMonitor:
    """Tracks recent network-class errors and probes the root on demand.

    Thread-safe. The orchestrator pokes `record_error` from the writer
    thread and queries `is_disconnected` from the main thread.
    """

    def __init__(
        self,
        root_path: str,
        *,
        error_threshold: int = 50,
        window_s: float = 10.0,
        recheck_interval_s: float = 2.0,
    ) -> None:
        self.root_path = root_path
        self.error_threshold = error_threshold
        self.window_s = window_s
        self.recheck_interval_s = recheck_interval_s
        self._timestamps: Deque[float] = deque()
        self._lock = threading.Lock()
        self._last_probe_at: float = 0.0
        self._last_probe_result: bool = False  # True = root is reachable

    def record_error(self, reason: str) -> None:
        if not _looks_like_network_error(reason):
            return
        now = time.monotonic()
        with self._lock:
            self._timestamps.append(now)
            self._gc(now)

    def recent_error_count(self) -> int:
        with self._lock:
            self._gc(time.monotonic())
            return len(self._timestamps)

    def _gc(self, now: float) -> None:
        cutoff = now - self.window_s
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    def _probe_root(self) -> bool:
        """True if the root path is currently reachable (single stat)."""
        try:
            os.stat(self.root_path)
            return True
        except OSError:
            return False

    def is_disconnected(self) -> bool:
        """Cheap path: returns False unless the error threshold is hit.

        On threshold, probe the root at most once per `recheck_interval_s`
        to avoid hammering the share when it's already struggling.
        """
        if self.recent_error_count() < self.error_threshold:
            return False
        now = time.monotonic()
        with self._lock:
            if now - self._last_probe_at < self.recheck_interval_s:
                return not self._last_probe_result
            self._last_probe_at = now
        reachable = self._probe_root()
        with self._lock:
            self._last_probe_result = reachable
        return not reachable

    def advisory(self) -> Optional[str]:
        """Human-readable hint when error rate is elevated but the share
        is still up — surfaced in the CLI completion line so users know
        to lower --workers or check the server.
        """
        n = self.recent_error_count()
        if n < max(10, self.error_threshold // 4):
            return None
        return (
            f"high error rate: {n} network-class errors in the last "
            f"{int(self.window_s)}s — consider lowering --workers"
        )
