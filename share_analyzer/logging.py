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
    """Thread-safe, human-readable progress for the console."""

    def __init__(self, every: float = 1.0, stream=sys.stderr) -> None:
        self._every = every
        self._stream = stream
        self._lock = threading.Lock()
        self._last_emit = 0.0
        self._start = time.monotonic()
        self._count = 0
        self._errors = 0
        self._bytes = 0

    def update(self, files: int = 0, errors: int = 0, byte_delta: int = 0,
               force: bool = False) -> None:
        with self._lock:
            self._count += files
            self._errors += errors
            self._bytes += byte_delta
            now = time.monotonic()
            if not force and now - self._last_emit < self._every:
                return
            self._last_emit = now
            elapsed = max(now - self._start, 1e-6)
            rate = self._count / elapsed
            mb = self._bytes / (1024 * 1024)
            self._stream.write(
                f"\r[scan] {self._count:>9,} files  {mb:>10,.1f} MB  "
                f"{rate:>7,.1f} f/s  errors={self._errors:<6}"
            )
            self._stream.flush()

    def finish(self) -> None:
        self.update(force=True)
        self._stream.write("\n")
        self._stream.flush()
