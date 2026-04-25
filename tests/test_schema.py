"""Schema migration sanity checks."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from share_analyzer.index.schema import CURRENT_VERSION, init_db, migrate


def test_init_creates_all_tables(tmp_path: Path) -> None:
    db = tmp_path / "schema.sqlite"
    conn = init_db(db)
    rows = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    expected = {
        "schema_version", "crawl_runs", "files", "crawl_errors",
        "crawl_checkpoint", "folders", "mime_categories",
    }
    assert expected.issubset(rows)
    views = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='view'"
    )}
    assert "duplicates" in views


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "schema.sqlite"
    conn = init_db(db)
    v1 = migrate(conn)
    v2 = migrate(conn)
    assert v1 == v2 == CURRENT_VERSION
    versions = [r[0] for r in conn.execute(
        "SELECT version FROM schema_version ORDER BY version"
    )]
    # Each migration recorded exactly once.
    assert versions == sorted(set(versions))


def test_wal_mode_enabled(tmp_path: Path) -> None:
    db = tmp_path / "schema.sqlite"
    conn = init_db(db)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_indexes_exist(tmp_path: Path) -> None:
    db = tmp_path / "schema.sqlite"
    conn = init_db(db)
    idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='files'"
    )}
    for needed in ("idx_files_parent", "idx_files_extension",
                   "idx_files_mtime", "idx_files_size", "idx_files_sha256"):
        assert needed in idx
