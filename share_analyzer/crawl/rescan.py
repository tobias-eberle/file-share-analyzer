"""Incremental rescan support.

Loads the previous run's file metadata into an in-memory map keyed by
path. The orchestrator uses it to short-circuit fingerprinting for
files whose `(size, mtime)` matches the prior snapshot.

Memory: ~150 bytes per file × 1M files ≈ 150 MB. Acceptable for phase 2;
a streaming merge against a sorted walker is a follow-up if needed.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from share_analyzer.crawl.fingerprint import Fingerprint
from share_analyzer.crawl.walker import FileEntry


# SMB protocol versions disagree on timestamp resolution: SMB1 rounds to
# 100 ns, FAT to 2 s, NTFS to 100 ns, ext4 to 1 ns. A share remounted
# under a different protocol can therefore round mtimes differently
# between two scans of the same unmodified file. With size unchanged,
# treat any mtime within this many seconds as identical.
DEFAULT_MTIME_TOLERANCE_S: float = 2.0


def _to_epoch(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


@dataclass(slots=True)
class _PriorFile:
    size: int
    mtime: Optional[str]
    mtime_epoch: Optional[float]
    sha256: Optional[str]
    mime_type: Optional[str]
    mime_category: Optional[str]
    owner: Optional[str]


def latest_completed_run(conn: sqlite3.Connection,
                         root_path: Optional[str] = None) -> Optional[dict]:
    """Most recent completed run, optionally constrained to a root_path."""
    if root_path is None:
        row = conn.execute(
            """
            SELECT id, root_path FROM crawl_runs
             WHERE status = 'completed'
             ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT id, root_path FROM crawl_runs
             WHERE status = 'completed' AND root_path = ?
             ORDER BY id DESC LIMIT 1
            """,
            (root_path,),
        ).fetchone()
    return dict(row) if row else None


class RescanContext:
    """In-memory index of the previous run's files, for delta detection.

    `classify` returns one of:
        ('unchanged', prior_fingerprint)  — reuse the prior sha256 + MIME
        ('modified',  None)               — re-fingerprint
        ('added',     None)               — re-fingerprint
    """

    def __init__(self, conn: sqlite3.Connection, previous_run_id: int,
                 mtime_tolerance_s: float = DEFAULT_MTIME_TOLERANCE_S) -> None:
        self.previous_run_id = previous_run_id
        self.mtime_tolerance_s = mtime_tolerance_s
        self._prior: dict[str, _PriorFile] = {}
        for r in conn.execute(
            """
            SELECT path, size, mtime, sha256, mime_type, mime_category, owner
            FROM files
            WHERE run_id = ? AND state != 'deleted'
            """,
            (previous_run_id,),
        ):
            self._prior[r["path"]] = _PriorFile(
                size=r["size"],
                mtime=r["mtime"],
                mtime_epoch=_to_epoch(r["mtime"]),
                sha256=r["sha256"],
                mime_type=r["mime_type"],
                mime_category=r["mime_category"],
                owner=r["owner"],
            )

    def __len__(self) -> int:
        return len(self._prior)

    def _mtime_close(self, prior: _PriorFile, current: Optional[str]) -> bool:
        if prior.mtime is None and current is None:
            return True
        if prior.mtime is None or current is None:
            return False
        if prior.mtime == current:
            return True  # exact ISO match — common case, no parse cost
        b = _to_epoch(current)
        if prior.mtime_epoch is None or b is None:
            return False
        return abs(prior.mtime_epoch - b) <= self.mtime_tolerance_s

    def classify(self, entry: FileEntry) -> tuple[str, Optional[Fingerprint]]:
        prior = self._prior.get(entry.path)
        if prior is None:
            return "added", None
        # Size mismatch is conclusive — content definitely differs.
        # mtime within tolerance + same size = treat as unchanged so a
        # protocol-rounding artefact doesn't trigger a full rehash.
        if prior.size == entry.size and self._mtime_close(prior, entry.mtime):
            return "unchanged", Fingerprint(
                sha256=prior.sha256,
                mime_type=prior.mime_type,
                mime_category=prior.mime_category,
                owner=prior.owner,
            )
        return "modified", None
