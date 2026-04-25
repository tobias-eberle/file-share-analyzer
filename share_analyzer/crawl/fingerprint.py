"""Fingerprinter — computes SHA-256 (with size cap) and MIME type per file.

Streams the file in chunks so memory stays bounded even for 100 MB files.
"""
from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol

from share_analyzer.crawl.walker import FileEntry
from share_analyzer.index.mime import MimeDetector, categorize


@dataclass(slots=True)
class Fingerprint:
    sha256: Optional[str]
    mime_type: Optional[str]
    mime_category: Optional[str]
    owner: Optional[str]
    error: Optional[str] = None


class Fingerprinter(Protocol):
    def fingerprint(self, entry: FileEntry) -> Fingerprint: ...


_LONG_PATH = sys.platform == "win32"


def _open_path(path: str) -> str:
    if not _LONG_PATH:
        return path
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


class StreamingFingerprinter:
    """Hashes content with a configurable size cap and detects MIME via libmagic.

    For files above hash_cap_bytes, sha256 is None — those are typically
    media/archives that aren't RAG candidates anyway.
    """

    HEAD_BYTES = 8192
    HASH_CHUNK = 1024 * 1024  # 1 MiB

    def __init__(self, hash_cap_bytes: int = 100 * 1024 * 1024) -> None:
        self.hash_cap_bytes = hash_cap_bytes
        self._mime = MimeDetector()

    def fingerprint(self, entry: FileEntry) -> Fingerprint:
        path = _open_path(entry.path)
        head = b""
        sha = None
        try:
            with open(path, "rb") as f:
                head = f.read(self.HEAD_BYTES)
                if entry.size <= self.hash_cap_bytes:
                    h = hashlib.sha256()
                    h.update(head)
                    while True:
                        chunk = f.read(self.HASH_CHUNK)
                        if not chunk:
                            break
                        h.update(chunk)
                    sha = h.hexdigest()
        except (PermissionError, FileNotFoundError, OSError) as e:
            mime = self._mime.detect(entry.path)  # extension fallback only
            return Fingerprint(
                sha256=None,
                mime_type=mime,
                mime_category=categorize(mime),
                owner=None,
                error=f"{type(e).__name__}: {e}",
            )
        mime = self._mime.detect(entry.path, head=head if head else None)
        return Fingerprint(
            sha256=sha,
            mime_type=mime,
            mime_category=categorize(mime),
            owner=None,
        )
