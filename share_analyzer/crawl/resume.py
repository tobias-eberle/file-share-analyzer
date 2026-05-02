"""Resume support — continue a previously-paused run.

Different intent from rescan:

  rescan       previous run is `completed`; new run gets a new run_id
               and classifies files as added/modified/unchanged/deleted.

  resume       previous run is `paused`; we KEEP the same run_id and
               just skip the paths already indexed. No state
               classification, no fingerprint reuse logic — we only
               need to know "have we already seen this path?"

Memory: ~150 B × N files in the path set. Acceptable up to ~1M files.
A streaming merge against a sorted walker would beat this; phase-2
follow-up if it becomes a real ceiling.
"""
from __future__ import annotations

import sqlite3
from typing import Iterator


class ResumeContext:
    """In-memory set of paths already indexed in this run."""

    def __init__(self, conn: sqlite3.Connection, run_id: int) -> None:
        self.run_id = run_id
        # state != 'deleted' so we don't accidentally re-index a path
        # that was carried forward from an earlier rescan.
        self._indexed: set[str] = {
            r[0]
            for r in conn.execute(
                "SELECT path FROM files WHERE run_id = ? AND state != 'deleted'",
                (run_id,),
            )
        }

    def __contains__(self, path: str) -> bool:
        return path in self._indexed

    def __len__(self) -> int:
        return len(self._indexed)

    def already_indexed(self) -> Iterator[str]:
        return iter(self._indexed)
