"""Tags carry through scan → SQLite → rescan → RAG export."""
from __future__ import annotations

import json
from pathlib import Path

from share_analyzer.crawl.orchestrator import CrawlOptions, run_crawl
from share_analyzer.index.schema import connect


def _build(tmp_path: Path) -> Path:
    root = tmp_path / "share"
    docs = root / "Projects" / "alpha" / "docs"
    docs.mkdir(parents=True)
    (docs / "notes.md").write_text("# Hello")
    (docs / "report.txt").write_text("data")
    archive = root / "_archive"
    archive.mkdir()
    (archive / "old.txt").write_text("old")  # _archive folder skipped in tags
    return root


def test_scan_populates_tags_column(tmp_path: Path):
    root = _build(tmp_path)
    db = tmp_path / "i.sqlite"
    result = run_crawl(root, db, CrawlOptions(workers=2))

    with connect(db) as conn:
        rows = conn.execute(
            "SELECT path, tags FROM files WHERE run_id = ? ORDER BY path",
            (result.run_id,),
        ).fetchall()

    assert rows, "expected at least one indexed file"
    by_name = {Path(r["path"]).name: r for r in rows}

    notes = json.loads(by_name["notes.md"]["tags"])
    # Path includes …/Projects/alpha/docs/notes.md — the tmp_path
    # parent segments (e.g. /tmp, /pytest-of-…) also become tags.
    # We assert the *meaningful* tail rather than the absolute set.
    assert "projects" in notes
    assert "alpha" in notes
    assert "docs" in notes

    # Files under `_archive` skip the underscore-prefix folder but
    # still tag the rest of the path.
    old = json.loads(by_name["old.txt"]["tags"])
    assert "_archive" not in old
    assert "archive" not in old


def test_rescan_unchanged_files_still_have_tags(tmp_path: Path):
    """Unchanged-on-rescan rows go through the sink with the new
    state='unchanged'; tags must populate even though the
    fingerprinter was bypassed."""
    root = _build(tmp_path)
    db = tmp_path / "i.sqlite"
    base = run_crawl(root, db, CrawlOptions(workers=2))
    rescan = run_crawl(
        root, db, CrawlOptions(workers=2, previous_run_id=base.run_id),
    )

    with connect(db) as conn:
        rows = conn.execute(
            "SELECT path, tags, state FROM files WHERE run_id = ?",
            (rescan.run_id,),
        ).fetchall()

    assert rows
    for r in rows:
        if r["state"] == "deleted":
            continue
        assert r["tags"] is not None, f"missing tags for {r['path']}"
        parsed = json.loads(r["tags"])
        assert isinstance(parsed, list)


def test_deleted_rows_carry_tags_from_prior_run(tmp_path: Path):
    """When a file vanishes, the synthesised 'deleted' row must keep
    the prior run's tags so churn reports can group by tag."""
    root = _build(tmp_path)
    db = tmp_path / "i.sqlite"
    base = run_crawl(root, db, CrawlOptions(workers=2))

    deleted = root / "Projects" / "alpha" / "docs" / "notes.md"
    deleted.unlink()
    rescan = run_crawl(
        root, db, CrawlOptions(workers=2, previous_run_id=base.run_id),
    )

    with connect(db) as conn:
        row = conn.execute(
            "SELECT tags FROM files WHERE run_id = ? AND path = ? AND state = 'deleted'",
            (rescan.run_id, str(deleted)),
        ).fetchone()
    assert row is not None
    assert row["tags"] is not None
    tags = json.loads(row["tags"])
    assert "docs" in tags
    assert "alpha" in tags


def test_rag_candidates_jsonl_includes_tags(tmp_path: Path):
    root = tmp_path / "share"
    docs = root / "Projects" / "alpha" / "docs"
    docs.mkdir(parents=True)
    (docs / "notes.md").write_text(
        "# A real note\n" + ("content " * 200), encoding="utf-8"
    )

    db = tmp_path / "i.sqlite"
    result = run_crawl(root, db, CrawlOptions(workers=2))

    from share_analyzer.reports import run_report
    out = tmp_path / "out"
    with connect(db) as conn:
        run_report("rag_candidates", conn, result.run_id, out, "all")

    lines = (out / "rag_candidates.jsonl").read_text(encoding="utf-8").splitlines()
    assert lines
    parsed = [json.loads(l) for l in lines]
    for entry in parsed:
        assert "tags" in entry
        assert isinstance(entry["tags"], list)
    # The single candidate should carry the meaningful folder tags.
    notes_entry = next(p for p in parsed if p["path"].endswith("notes.md"))
    assert "projects" in notes_entry["tags"]
    assert "alpha" in notes_entry["tags"]
    assert "docs" in notes_entry["tags"]


def test_v3_migration_adds_tags_column(tmp_path: Path):
    """A v2 database (created by manually applying migrations 1+2)
    must upgrade cleanly to v3 with the new tags column populated as
    NULL on legacy rows."""
    import sqlite3
    from datetime import datetime, timezone

    from share_analyzer.index.schema import (
        CURRENT_VERSION, _MIGRATIONS, init_db,
    )

    db = tmp_path / "i.sqlite"
    conn = sqlite3.connect(str(db), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    for version in (1, 2):
        for stmt in _MIGRATIONS[version]:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, datetime.now(timezone.utc).isoformat()),
        )
    conn.execute(
        "INSERT INTO crawl_runs (root_path, started_at, status) "
        "VALUES (?, ?, 'completed')",
        ("/tmp/legacy", datetime.now(timezone.utc).isoformat()),
    )
    rid = conn.execute("SELECT MAX(id) FROM crawl_runs").fetchone()[0]
    conn.execute(
        """
        INSERT INTO files (
            run_id, path, parent_path, depth, name, extension, size,
            mtime, atime, ctime, sha256, mime_type, mime_category,
            owner, state
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'baseline')
        """,
        (rid, "/tmp/legacy/a.txt", "/tmp/legacy", 1, "a.txt", ".txt", 4,
         None, None, None, None, "text/plain", "text-extractable", None),
    )
    conn.close()

    # Reopen via init_db — should run v3 migration.
    conn = init_db(db)
    versions = {r[0] for r in conn.execute("SELECT version FROM schema_version")}
    assert versions == set(range(1, CURRENT_VERSION + 1))

    # Pre-existing row's tags column is NULL (no value → no JSON).
    tags = conn.execute(
        "SELECT tags FROM files WHERE path = '/tmp/legacy/a.txt'"
    ).fetchone()[0]
    assert tags is None
    conn.close()
