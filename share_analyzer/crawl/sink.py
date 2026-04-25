"""Sink — batches file rows into the SQLite index."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Protocol

from share_analyzer.crawl.fingerprint import Fingerprint
from share_analyzer.crawl.walker import FileEntry, WalkError
from share_analyzer.index.schema import init_db
from share_analyzer.index.mime import seed_mime_categories


class Sink(Protocol):
    def begin_run(self, root_path: str, *, workers: int, hash_cap_bytes: int) -> int: ...
    def write_files(self, rows: Iterable[tuple[FileEntry, Fingerprint]]) -> int: ...
    def write_errors(self, errors: Iterable[WalkError]) -> int: ...
    def checkpoint(self, last_path: Optional[str], files_processed: int) -> None: ...
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

    def begin_run(self, root_path: str, *, workers: int, hash_cap_bytes: int) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO crawl_runs (root_path, started_at, status, workers, hash_cap_bytes)
            VALUES (?, ?, 'running', ?, ?)
            """,
            (root_path, datetime.now(timezone.utc).isoformat(), workers, hash_cap_bytes),
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

    def write_files(self, rows: Iterable[tuple[FileEntry, Fingerprint]]) -> int:
        run_id = self.run_id
        n = 0
        for entry, fp in rows:
            self._buf_files.append((
                run_id, entry.path, entry.parent_path, entry.depth,
                entry.name, entry.extension, entry.size,
                entry.mtime, entry.atime, entry.ctime,
                fp.sha256, fp.mime_type, fp.mime_category, fp.owner,
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
                sha256, mime_type, mime_category, owner
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def close(self) -> None:
        self._flush_files()
        self._flush_errors()
        self.conn.close()
