"""Filesystem traversal — Walker interface plus the phase-1 scandir impl.

Phase 2 will add SmbDirectWalker for unmounted shares without changing
the orchestrator or sink.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterator, Protocol, Sequence


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
    - Detects symlink loops via a visited inode set.
    - Uses Windows long-path prefix when applicable.
    """

    def __init__(self, root: str | Path, *,
                 exclude_globs: Sequence[str] = (),
                 follow_symlinks: bool = False) -> None:
        self.root = str(Path(root))
        self.exclude_globs = tuple(exclude_globs)
        self.follow_symlinks = follow_symlinks

    def _excluded(self, name: str, full: str) -> bool:
        if not self.exclude_globs:
            return False
        return any(fnmatch(name, g) or fnmatch(full, g) for g in self.exclude_globs)

    def walk(self) -> Iterator[FileEntry | WalkError]:
        root = self.root
        visited: set[tuple[int, int]] = set()
        stack: list[tuple[str, int]] = [(root, 0)]
        while stack:
            current, depth = stack.pop()
            scan_target = _long_path(current)
            try:
                it = os.scandir(scan_target)
            except (PermissionError, FileNotFoundError, NotADirectoryError, OSError) as e:
                yield WalkError(path=current, reason=f"{type(e).__name__}: {e}")
                continue

            with it:
                for entry in it:
                    full = os.path.join(current, entry.name)
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
                        # Symlink loops are only possible when actually
                        # following symlinks. With follow_symlinks=False
                        # the loop guard offers no protection — and on
                        # Windows / SMB shares `st_ino` is frequently 0
                        # (the file-id API isn't fully supported on remote
                        # filesystems), which would falsely collapse every
                        # directory into a single "symlink-loop" error.
                        if self.follow_symlinks:
                            try:
                                st = entry.stat(follow_symlinks=True)
                            except OSError as e:
                                yield WalkError(path=full, reason=f"stat: {e}")
                                continue
                            if st.st_ino:
                                key = (st.st_dev, st.st_ino)
                                if key in visited:
                                    yield WalkError(path=full, reason="symlink-loop")
                                    continue
                                visited.add(key)
                        stack.append((full, depth + 1))
                    elif is_file:
                        emitted = self._emit_file(full, current, depth + 1, entry)
                        if emitted is not None:
                            yield emitted
                    else:
                        yield WalkError(path=full, reason="non-regular file")

    @staticmethod
    def _emit_file(full: str, parent: str, depth: int, entry) -> FileEntry | WalkError:
        try:
            st = entry.stat(follow_symlinks=False)
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
