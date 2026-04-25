"""Schema migrations apply cleanly to a v1 database."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from share_analyzer.index.schema import CURRENT_VERSION, _MIGRATIONS, init_db


def _apply_through(db_path: Path, target_version: int) -> sqlite3.Connection:
    """Apply migrations [1..target_version] without going through init_db,
    so we can simulate an old-version database.
    """
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    for version in range(1, target_version + 1):
        for stmt in _MIGRATIONS[version]:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, datetime.now(timezone.utc).isoformat()),
        )
    return conn


def test_v1_db_migrates_to_current(tmp_path: Path):
    db = tmp_path / "i.sqlite"
    conn = _apply_through(db, 1)
    # Insert a v1-style file row (no `state` column yet).
    conn.execute(
        """
        INSERT INTO crawl_runs (root_path, started_at, status, file_count, error_count)
        VALUES (?, ?, 'completed', 1, 0)
        """,
        ("/tmp/legacy", datetime.now(timezone.utc).isoformat()),
    )
    run_id = conn.execute("SELECT MAX(id) FROM crawl_runs").fetchone()[0]
    conn.execute(
        """
        INSERT INTO files (
            run_id, path, parent_path, depth, name, extension, size,
            mtime, atime, ctime, sha256, mime_type, mime_category, owner
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, "/tmp/legacy/a.txt", "/tmp/legacy", 1, "a.txt", ".txt", 4,
         None, None, None, None, "text/plain", "text-extractable", None),
    )
    conn.close()

    # Reopen via init_db — should run v2 migration cleanly.
    conn = init_db(db)
    versions = {r[0] for r in conn.execute("SELECT version FROM schema_version")}
    assert versions == set(range(1, CURRENT_VERSION + 1))

    # Existing rows defaulted to 'baseline'; v2 indexes exist.
    state = conn.execute(
        "SELECT state FROM files WHERE path = ?", ("/tmp/legacy/a.txt",)
    ).fetchone()[0]
    assert state == "baseline"

    indexes = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='files'"
    )}
    for needed in ("idx_files_run_parent", "idx_files_run_mime",
                   "idx_files_run_sha256", "idx_files_run_state"):
        assert needed in indexes
    conn.close()
