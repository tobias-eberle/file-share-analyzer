"""Filesystem traversal — Walker interface plus the phase-1/2 scandir impl.

`LocalScandirWalker` runs sequentially when `dir_workers <= 1` (the
phase-1 default, easy to debug) and in parallel when set higher (phase-2
default for SMB latency). Both modes share `_process_directory` so the
SMB quirks — long paths, retry, `st_ino == 0` symlink-loop guard — are
fixed in one place.

A future `SmbDirectWalker` for unmounted shares can implement the same
`Walker` protocol without touching the orchestrator or sink.
"""
from __future__ import annotations

import logging
import os
import queue
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional, Protocol, Sequence

from share_analyzer.crawl.retry import DEFAULT_BACKOFF, with_retry

log = logging.getLogger("share_analyzer.crawl.walker")


@dataclass(slots=True)
class FileEntry:
    path: str
    parent_path: str
    depth: int
    name: str
    extension: str
    size: int
    mtime: str | None
    atime: str | None
    ctime: str | None


@dataclass(slots=True)
class WalkError:
    path: str
    reason: str


@dataclass(slots=True)
class _SubDir:
    """Internal — a directory that needs further traversal."""
    path: str
    depth: int


class Walker(Protocol):
    def walk(self) -> Iterator[FileEntry | WalkError]: ...


def _to_iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _long_path(path: str) -> str:
    """On Windows, prefix with \\?\\ to bypass MAX_PATH (260 chars)."""
    if sys.platform != "win32":
        return path
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


class LocalScandirWalker:
    """Recursive os.scandir walker for a mounted path.

    - Continues on permission denied and broken symlinks (yields WalkError).
    - Detects symlink loops via a visited inode set (only when
      `follow_symlinks=True` — otherwise loops are structurally
      impossible — and only when `st_ino != 0`, since SMB often returns
      0 for the inode and would otherwise false-positive every sibling).
    - Uses Windows long-path prefix when applicable.
    - Retries transient errors per `retry_backoff`.
    - Parallelises directory enumeration across `dir_workers` threads
      when set above 1.
    """

    def __init__(self, root: str | Path, *,
                 exclude_globs: Sequence[str] = (),
                 excluded_paths: Iterable[str] = (),
                 follow_symlinks: bool = False,
                 retry_backoff: Sequence[float] = DEFAULT_BACKOFF,
                 dir_workers: int = 1) -> None:
        self.root = str(Path(root))
        self.exclude_globs = tuple(exclude_globs)
        # Path-exact exclusions from the UI tree picker. Distinct from
        # `exclude_globs`: globs match anywhere by name/pattern; this
        # set is "skip these specific subtrees of the chosen root."
        # Normalised once here so per-entry lookups are pure dict-hits.
        self.excluded_paths: frozenset[str] = frozenset(
            str(Path(p)) for p in excluded_paths
        )
        self.follow_symlinks = follow_symlinks
        self.retry_backoff = tuple(retry_backoff)
        self.dir_workers = max(1, dir_workers)

    def _excluded(self, name: str, full: str) -> bool:
        if full in self.excluded_paths:
            return True
        if not self.exclude_globs:
            return False
        return any(fnmatch(name, g) or fnmatch(full, g) for g in self.exclude_globs)

    def _retry(self, fn: Callable[[], object], path: str) -> object:
        def _on_retry(attempt: int, exc: BaseException, sleep_s: float) -> None:
            log.info("retry", extra={
                "path": path, "attempt": attempt,
                "errno": getattr(exc, "errno", None),
                "winerror": getattr(exc, "winerror", None),
                "sleep_s": sleep_s,
                "exc": f"{type(exc).__name__}: {exc}",
            })
        return with_retry(fn, backoff=self.retry_backoff, on_retry=_on_retry)

    def walk(self) -> Iterator[FileEntry | WalkError]:
        if self.dir_workers == 1:
            yield from self._walk_sequential()
        else:
            yield from self._walk_parallel()

    # ------------------------------------------------------------------
    # Sequential traversal
    # ------------------------------------------------------------------

    def _walk_sequential(self) -> Iterator[FileEntry | WalkError]:
        visited: set[tuple[int, int]] = set()
        visited_lock = threading.Lock()  # unused here; passed for shape
        stack: list[_SubDir] = [_SubDir(self.root, 0)]
        while stack:
            current = stack.pop()
            for item in self._process_directory(current, visited, visited_lock):
                if isinstance(item, _SubDir):
                    stack.append(item)
                else:
                    yield item

    # ------------------------------------------------------------------
    # Parallel traversal
    # ------------------------------------------------------------------

    def _walk_parallel(self) -> Iterator[FileEntry | WalkError]:
        """Worker pool that consumes a directory queue and emits results.

        Termination uses queue.Queue's unfinished-task counter: the root
        is one task; every directory pushed by a worker is another task.
        When all are done, a watcher thread sees `dir_q.join()` return
        and pushes a sentinel onto `result_q` so iteration ends.
        """
        dir_workers = self.dir_workers
        # Bound result_q so workers exert backpressure on a slow consumer.
        result_q: queue.Queue = queue.Queue(maxsize=8192)
        dir_q: queue.Queue = queue.Queue()
        dir_q.put(_SubDir(self.root, 0))
        visited: set[tuple[int, int]] = set()
        visited_lock = threading.Lock()
        stop = threading.Event()
        worker_exc: list[BaseException] = []

        def worker() -> None:
            try:
                while True:
                    item = dir_q.get()
                    if item is None:
                        dir_q.task_done()
                        return
                    try:
                        if stop.is_set():
                            continue
                        for emitted in self._process_directory(
                            item, visited, visited_lock,
                        ):
                            if stop.is_set():
                                break
                            if isinstance(emitted, _SubDir):
                                dir_q.put(emitted)
                            else:
                                # `put` blocks if consumer is slow —
                                # natural backpressure, no abort needed.
                                result_q.put(emitted)
                    except BaseException as e:  # pragma: no cover — defensive
                        worker_exc.append(e)
                        stop.set()
                    finally:
                        dir_q.task_done()
            except BaseException as e:  # pragma: no cover — defensive
                worker_exc.append(e)
                stop.set()

        threads = [
            threading.Thread(target=worker, name=f"walk-{i}", daemon=True)
            for i in range(dir_workers)
        ]
        for t in threads:
            t.start()

        _SENTINEL: object = object()

        def watcher() -> None:
            dir_q.join()
            result_q.put(_SENTINEL)
            for _ in threads:
                dir_q.put(None)

        threading.Thread(target=watcher, name="walk-watcher", daemon=True).start()

        try:
            while True:
                item = result_q.get()
                if item is _SENTINEL:
                    break
                yield item
        finally:
            stop.set()
            for t in threads:
                t.join()
            if worker_exc:
                raise worker_exc[0]

    # ------------------------------------------------------------------
    # Per-directory processing — shared by both modes
    # ------------------------------------------------------------------

    def _process_directory(
        self,
        sub: _SubDir,
        visited: set[tuple[int, int]],
        visited_lock: threading.Lock,
    ) -> Iterable[FileEntry | WalkError | _SubDir]:
        """Yield results for a single directory's contents.

        Returns an iterable so callers can handle the items however they
        like (push to stack, push to queue, drop, etc.). Subdirectories
        come back as `_SubDir` and the caller is responsible for
        recursion.
        """
        scan_target = _long_path(sub.path)
        try:
            it = self._retry(lambda: os.scandir(scan_target), sub.path)
        except (PermissionError, FileNotFoundError, NotADirectoryError, OSError) as e:
            yield WalkError(path=sub.path, reason=f"{type(e).__name__}: {e}")
            return

        with it:
            for entry in it:
                full = os.path.join(sub.path, entry.name)
                if self._excluded(entry.name, full):
                    continue
                try:
                    is_symlink = entry.is_symlink()
                except OSError as e:
                    yield WalkError(path=full, reason=f"{type(e).__name__}: {e}")
                    continue
                if is_symlink and not self.follow_symlinks:
                    yield WalkError(path=full, reason="symlink-skipped")
                    continue
                try:
                    is_dir = entry.is_dir(follow_symlinks=self.follow_symlinks)
                    is_file = entry.is_file(follow_symlinks=self.follow_symlinks)
                except OSError as e:
                    yield WalkError(path=full, reason=f"{type(e).__name__}: {e}")
                    continue

                if is_dir:
                    if self.follow_symlinks:
                        try:
                            st = self._retry(
                                lambda: entry.stat(follow_symlinks=True),
                                full,
                            )
                        except OSError as e:
                            yield WalkError(path=full, reason=f"stat: {e}")
                            continue
                        if st.st_ino:
                            key = (st.st_dev, st.st_ino)
                            with visited_lock:
                                if key in visited:
                                    yield WalkError(path=full, reason="symlink-loop")
                                    continue
                                visited.add(key)
                    yield _SubDir(full, sub.depth + 1)
                elif is_file:
                    emitted = self._emit_file(full, sub.path, sub.depth + 1, entry)
                    if emitted is not None:
                        yield emitted
                else:
                    yield WalkError(path=full, reason="non-regular file")

    def _emit_file(self, full: str, parent: str, depth: int, entry) -> FileEntry | WalkError:
        try:
            st = self._retry(lambda: entry.stat(follow_symlinks=False), full)
        except OSError as e:
            return WalkError(path=full, reason=f"stat: {e}")
        name = entry.name
        ext = os.path.splitext(name)[1].lower()
        return FileEntry(
            path=full,
            parent_path=parent,
            depth=depth,
            name=name,
            extension=ext,
            size=st.st_size,
            mtime=_to_iso(st.st_mtime),
            atime=_to_iso(st.st_atime),
            ctime=_to_iso(st.st_ctime),
        )
