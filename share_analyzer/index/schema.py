"""SQLite schema with versioned migrations.

The schema_version table records every applied migration so phase 2 can
add migrations without breaking phase 1 databases.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

CURRENT_VERSION = 1


_MIGRATIONS: dict[int, list[str]] = {
    1: [
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS crawl_runs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            root_path       TEXT NOT NULL,
            started_at      TEXT NOT NULL,
            completed_at    TEXT,
            file_count      INTEGER NOT NULL DEFAULT 0,
            error_count     INTEGER NOT NULL DEFAULT 0,
            status          TEXT NOT NULL DEFAULT 'running',
            workers         INTEGER,
            hash_cap_bytes  INTEGER
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS files (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id         INTEGER NOT NULL REFERENCES crawl_runs(id),
            path           TEXT NOT NULL,
            parent_path    TEXT NOT NULL,
            depth          INTEGER NOT NULL,
            name           TEXT NOT NULL,
            extension      TEXT,
            size           INTEGER NOT NULL,
            mtime          TEXT,
            atime          TEXT,
            ctime          TEXT,
            sha256         TEXT,
            mime_type      TEXT,
            mime_category  TEXT,
            owner          TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_files_run        ON files(run_id)",
        "CREATE INDEX IF NOT EXISTS idx_files_parent     ON files(parent_path)",
        "CREATE INDEX IF NOT EXISTS idx_files_extension  ON files(extension)",
        "CREATE INDEX IF NOT EXISTS idx_files_mtime      ON files(mtime)",
        "CREATE INDEX IF NOT EXISTS idx_files_size       ON files(size)",
        "CREATE INDEX IF NOT EXISTS idx_files_sha256     ON files(sha256)",
        "CREATE INDEX IF NOT EXISTS idx_files_mime_cat   ON files(mime_category)",
        """
        CREATE TABLE IF NOT EXISTS crawl_errors (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id       INTEGER NOT NULL REFERENCES crawl_runs(id),
            path         TEXT NOT NULL,
            reason       TEXT NOT NULL,
            recorded_at  TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_crawl_errors_run ON crawl_errors(run_id)",
        """
        CREATE TABLE IF NOT EXISTS crawl_checkpoint (
            run_id           INTEGER PRIMARY KEY REFERENCES crawl_runs(id),
            last_path        TEXT,
            files_processed  INTEGER NOT NULL DEFAULT 0,
            updated_at       TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS folders (
            run_id                  INTEGER NOT NULL REFERENCES crawl_runs(id),
            path                    TEXT NOT NULL,
            parent_path             TEXT,
            depth                   INTEGER NOT NULL,
            file_count              INTEGER NOT NULL,
            total_size              INTEGER NOT NULL,
            max_depth_below         INTEGER NOT NULL,
            mtime_min               TEXT,
            mtime_max               TEXT,
            dominant_mime_category  TEXT,
            PRIMARY KEY (run_id, path)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent_path)",
        "CREATE INDEX IF NOT EXISTS idx_folders_depth  ON folders(depth)",
        """
        CREATE TABLE IF NOT EXISTS mime_categories (
            mime_type  TEXT PRIMARY KEY,
            category   TEXT NOT NULL
        )
        """,
        """
        CREATE VIEW IF NOT EXISTS duplicates AS
            SELECT
                run_id,
                sha256,
                COUNT(*)              AS file_count,
                MAX(size)             AS file_size,
                (COUNT(*) - 1) * MAX(size) AS wasted_bytes
            FROM files
            WHERE sha256 IS NOT NULL
            GROUP BY run_id, sha256
            HAVING COUNT(*) >= 2
        """,
    ],
}


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode and sane pragmas.

    `check_same_thread=False` lets the sink hand the connection to the
    single writer thread used by the orchestrator. Concurrency is
    serialized at the application level — there is never more than one
    writer active.
    """
    conn = sqlite3.connect(
        str(db_path), isolation_level=None, timeout=30.0,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    )
    if cur.fetchone() is None:
        return set()
    return {row[0] for row in conn.execute("SELECT version FROM schema_version")}


def migrate(conn: sqlite3.Connection) -> int:
    """Apply any missing migrations. Returns the resulting schema version."""
    applied = _applied_versions(conn)
    for version in sorted(_MIGRATIONS):
        if version in applied:
            continue
        conn.execute("BEGIN")
        try:
            for stmt in _MIGRATIONS[version]:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, datetime.now(timezone.utc).isoformat()),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return CURRENT_VERSION


def init_db(db_path: Path | str) -> sqlite3.Connection:
    conn = connect(db_path)
    migrate(conn)
    return conn
