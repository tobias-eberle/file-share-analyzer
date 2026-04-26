"""Structured JSON sidecar logging plus human-readable console progress."""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in ("args", "msg", "levelname", "levelno", "name", "pathname",
                      "filename", "module", "exc_info", "exc_text", "stack_info",
                      "lineno", "funcName", "created", "msecs", "relativeCreated",
                      "thread", "threadName", "processName", "process", "message",
                      "taskName"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(db_path: Path, verbose: bool = False) -> logging.Logger:
    """Configure JSON logging to a sidecar file next to the DB.

    Console handler stays minimal; progress output is printed separately.
    """
    log_path = db_path.with_suffix(db_path.suffix + ".log")
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("share_analyzer")
    root.handlers.clear()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(JsonFormatter())
    file_handler.setLevel(logging.DEBUG)
    root.addHandler(file_handler)

    if verbose:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        console.setLevel(logging.DEBUG)
        root.addHandler(console)

    root.propagate = False
    return root


class ProgressPrinter:
    """A single self-overwriting console line: counts + rate + last path.

    Three deliberately small ideas, each cheap:
      1. Callers pass *absolute* counts (`files`, `errors`) on every
         hot-path event — every batch flush, every error ingest. They
         don't worry about throttling.
      2. The printer rate-limits itself to one redraw per `every`
         seconds. So `update()` can be called thousands of times per
         second from the writer thread without touching stderr each
         time. The lock is uncontended in the common case.
      3. We carry a `last_path` so the user sees the scan moving
         through the tree — long paths are truncated from the LEFT
         (right-side stays visible, since the tail is the informative
         part). The previous line's length is remembered and padded
         over so a shorter line doesn't leave stale characters.

    Non-tty streams (CI logs, redirects) skip the `\\r` redraws and
    emit nothing here; the CLI's final summary is enough for those.
    """

    def __init__(self, every: float = 0.5, stream=sys.stderr,
                 path_width: int = 60) -> None:
        self._every = every
        self._stream = stream
        self._path_width = path_width
        self._lock = threading.Lock()
        self._start = time.monotonic()
        self._last_emit = 0.0
        self._files = 0
        self._errors = 0
        self._last_path = ""
        self._last_line_len = 0
        # Skip drawing on non-tty streams — \r in CI logs is ugly.
        self._enabled = bool(getattr(stream, "isatty", lambda: False)())

    @staticmethod
    def _truncate_left(path: str, width: int) -> str:
        if len(path) <= width:
            return path
        return "…" + path[-(width - 1):]

    def update(self, files: int, errors: int = 0,
               last_path: str = "", *, force: bool = False) -> None:
        with self._lock:
            self._files = files
            self._errors = errors
            if last_path:
                self._last_path = last_path
            now = time.monotonic()
            if not force and now - self._last_emit < self._every:
                return
            self._last_emit = now
            if self._enabled:
                self._render(now)

    def _render(self, now: float) -> None:
        elapsed = max(now - self._start, 1e-6)
        rate = self._files / elapsed
        path = self._truncate_left(self._last_path, self._path_width)
        line = (
            f"\r[scan] {self._files:>10,} files  "
            f"{rate:>7,.0f} f/s  "
            f"{self._errors:>4,} err  "
            f"{path:<{self._path_width}}"
        )
        # Pad over leftover characters from a previous, longer line.
        pad = max(0, self._last_line_len - len(line))
        self._stream.write(line + " " * pad)
        self._stream.flush()
        self._last_line_len = len(line)

    def finish(self) -> None:
        with self._lock:
            if self._enabled:
                self._render(time.monotonic())
                self._stream.write("\n")
                self._stream.flush()
