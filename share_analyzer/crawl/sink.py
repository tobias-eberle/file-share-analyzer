"""Sink — batches file rows into the SQLite index."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Protocol

from share_analyzer.crawl.fingerprint import Fingerprint
from share_analyzer.crawl.walker import FileEntry, WalkError
from share_analyzer.index.schema import init_db
from share_analyzer.index.mime import seed_mime_categories
from share_analyzer.tags import extract_tags


class Sink(Protocol):
    def begin_run(self, root_path: str, *, workers: int, hash_cap_bytes: int,
                  previous_run_id: Optional[int] = None) -> int: ...
    def resume_run(self, run_id: int) -> int: ...
    def write_files(self, rows: Iterable[tuple[FileEntry, Fingerprint, str]]) -> int: ...
    def write_errors(self, errors: Iterable[WalkError]) -> int: ...
    def checkpoint(self, last_path: Optional[str], files_processed: int) -> None: ...
    def wal_checkpoint(self) -> None: ...
    def end_run(self, *, file_count: int, error_count: int, status: str) -> None: ...
    def close(self) -> None: ...


class SqliteSink:
    """SQLite-backed sink. Single writer, batched inserts, WAL-mode."""

    BATCH_SIZE = 1000

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.conn: sqlite3.Connection = init_db(self.db_path)
        seed_mime_categories(self.conn)
        self._run_id: Optional[int] = None
        self._buf_files: list[tuple] = []
        self._buf_errors: list[tuple] = []

    @property
    def run_id(self) -> int:
        if self._run_id is None:
            raise RuntimeError("begin_run has not been called")
        return self._run_id

    def begin_run(self, root_path: str, *, workers: int, hash_cap_bytes: int,
                  previous_run_id: Optional[int] = None) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO crawl_runs (
                root_path, started_at, status, workers, hash_cap_bytes,
                previous_run_id
            ) VALUES (?, ?, 'running', ?, ?, ?)
            """,
            (root_path, datetime.now(timezone.utc).isoformat(),
             workers, hash_cap_bytes, previous_run_id),
        )
        self._run_id = cur.lastrowid
        self.conn.execute(
            """
            INSERT INTO crawl_checkpoint (run_id, last_path, files_processed, updated_at)
            VALUES (?, NULL, 0, ?)
            """,
            (self._run_id, datetime.now(timezone.utc).isoformat()),
        )
        return self._run_id

    def resume_run(self, run_id: int) -> int:
        """Re-bind to an existing paused run.

        Flips the row's status from 'paused' back to 'running' and
        clears `completed_at` (it was set by the previous pause). The
        orchestrator builds a `ResumeContext` separately to skip
        already-indexed paths; this method just owns the DB-state
        transition. Refuses any status other than 'paused' so a typo
        can't corrupt a completed/failed/disconnected run.
        """
        row = self.conn.execute(
            "SELECT status FROM crawl_runs WHERE id = ?", (run_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"run {run_id} not found")
        if row["status"] != "paused":
            raise ValueError(
                f"run {run_id} status is {row['status']!r}; "
                "resume only works on 'paused' runs"
            )
        self.conn.execute(
            "UPDATE crawl_runs SET status = 'running', completed_at = NULL "
            "WHERE id = ?",
            (run_id,),
        )
        self._run_id = run_id
        return run_id

    def write_files(self, rows: Iterable[tuple[FileEntry, Fingerprint, str]]) -> int:
        run_id = self.run_id
        n = 0
        for entry, fp, state in rows:
            tags_json = json.dumps(extract_tags(entry.path), ensure_ascii=False)
            self._buf_files.append((
                run_id, entry.path, entry.parent_path, entry.depth,
                entry.name, entry.extension, entry.size,
                entry.mtime, entry.atime, entry.ctime,
                fp.sha256, fp.mime_type, fp.mime_category, fp.owner,
                state, tags_json,
            ))
            n += 1
            if fp.error:
                self._buf_errors.append((
                    run_id, entry.path, fp.error,
                    datetime.now(timezone.utc).isoformat(),
                ))
            if len(self._buf_files) >= self.BATCH_SIZE:
                self._flush_files()
        return n

    def write_errors(self, errors: Iterable[WalkError]) -> int:
        run_id = self.run_id
        now = datetime.now(timezone.utc).isoformat()
        n = 0
        for err in errors:
            self._buf_errors.append((run_id, err.path, err.reason, now))
            n += 1
        if len(self._buf_errors) >= self.BATCH_SIZE:
            self._flush_errors()
        return n

    def _flush_files(self) -> None:
        if not self._buf_files:
            return
        self.conn.executemany(
            """
            INSERT INTO files (
                run_id, path, parent_path, depth,
                name, extension, size, mtime, atime, ctime,
                sha256, mime_type, mime_category, owner, state, tags
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            self._buf_files,
        )
        self._buf_files.clear()

    def _flush_errors(self) -> None:
        if not self._buf_errors:
            return
        self.conn.executemany(
            """
            INSERT INTO crawl_errors (run_id, path, reason, recorded_at)
            VALUES (?, ?, ?, ?)
            """,
            self._buf_errors,
        )
        self._buf_errors.clear()

    def checkpoint(self, last_path: Optional[str], files_processed: int) -> None:
        self._flush_files()
        self._flush_errors()
        self.conn.execute(
            """
            UPDATE crawl_checkpoint
               SET last_path = ?, files_processed = ?, updated_at = ?
             WHERE run_id = ?
            """,
            (
                last_path,
                files_processed,
                datetime.now(timezone.utc).isoformat(),
                self.run_id,
            ),
        )

    def wal_checkpoint(self) -> None:
        """Force-truncate the WAL so it can't grow without bound on long runs."""
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def end_run(self, *, file_count: int, error_count: int, status: str) -> None:
        self._flush_files()
        self._flush_errors()
        self.conn.execute(
            """
            UPDATE crawl_runs
               SET completed_at = ?, file_count = ?, error_count = ?, status = ?
             WHERE id = ?
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                file_count, error_count, status, self.run_id,
            ),
        )

    def copy_deleted_from_previous(self, previous_run_id: int) -> int:
        """Insert a 'deleted' row for every file in the previous run that
        wasn't seen in this run. Atomic single-statement insert.

        Tags carry forward from the prior row — they're a property of
        the path, and a deleted file's path doesn't change.
        """
        self._flush_files()
        cur = self.conn.execute(
            """
            INSERT INTO files (
                run_id, path, parent_path, depth, name, extension, size,
                mtime, atime, ctime, sha256, mime_type, mime_category,
                owner, state, tags
            )
            SELECT ?, prev.path, prev.parent_path, prev.depth, prev.name,
                   prev.extension, prev.size, prev.mtime, prev.atime,
                   prev.ctime, prev.sha256, prev.mime_type, prev.mime_category,
                   prev.owner, 'deleted', prev.tags
            FROM files prev
            LEFT JOIN files cur
                   ON cur.path = prev.path AND cur.run_id = ?
            WHERE prev.run_id = ?
              AND prev.state != 'deleted'
              AND cur.id IS NULL
            """,
            (self.run_id, self.run_id, previous_run_id),
        )
        return cur.rowcount or 0

    def close(self) -> None:
        self._flush_files()
        self._flush_errors()
        self.conn.close()
