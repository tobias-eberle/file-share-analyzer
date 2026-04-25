"""Crawler robustness: permission denied, unicode, symlink loops, basic counting."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from share_analyzer.crawl.orchestrator import CrawlOptions, run_crawl
from share_analyzer.crawl.walker import LocalScandirWalker, WalkError
from share_analyzer.index.schema import connect


def _file_count(db_path: Path, run_id: int) -> int:
    with connect(db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM files WHERE run_id = ?", (run_id,)
        ).fetchone()[0]


def test_basic_crawl_counts_match(crawled_db, fixture_share: Path):
    db, run_id = crawled_db
    fs_count = sum(1 for _ in fixture_share.rglob("*") if _.is_file())
    indexed = _file_count(db, run_id)
    assert indexed == fs_count


def test_duplicates_detected(crawled_db):
    db, run_id = crawled_db
    with connect(db) as conn:
        rows = conn.execute(
            "SELECT sha256, file_count FROM duplicates WHERE run_id = ?",
            (run_id,),
        ).fetchall()
    assert any(r["file_count"] >= 3 for r in rows), "expected 3-copy duplicate cluster"


def test_unicode_paths_round_trip(crawled_db):
    db, run_id = crawled_db
    with connect(db) as conn:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM files WHERE run_id = ? AND name LIKE '%fü%'",
            (run_id,),
        )]
    assert names, "unicode-named file should be indexed"


def test_long_path_round_trip(crawled_db):
    db, run_id = crawled_db
    with connect(db) as conn:
        long_paths = [r[0] for r in conn.execute(
            "SELECT path FROM files WHERE run_id = ? AND name = 'deep_file.txt'",
            (run_id,),
        )]
    assert len(long_paths) == 1


def test_mime_categories_assigned(crawled_db):
    db, run_id = crawled_db
    with connect(db) as conn:
        cats = {r[0] for r in conn.execute(
            "SELECT DISTINCT mime_category FROM files WHERE run_id = ?",
            (run_id,),
        ) if r[0]}
    # We seeded text/markdown, an exe, a zip — categorizer should produce
    # at least the text-extractable bucket plus one of archive/executable.
    assert "text-extractable" in cats
    assert cats & {"archive", "executable", "other"}


@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX permissions not enforced like this on Windows")
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root bypasses POSIX permissions")
def test_permission_denied_recorded(tmp_path: Path):
    root = tmp_path / "share"
    root.mkdir()
    (root / "open.txt").write_text("ok\n")
    locked = root / "locked"
    locked.mkdir()
    (locked / "secret.txt").write_text("nope\n")
    os.chmod(locked, 0o000)

    db = tmp_path / "i.sqlite"
    try:
        result = run_crawl(root, db, CrawlOptions(workers=1))
    finally:
        os.chmod(locked, 0o755)

    assert result.error_count >= 1
    with connect(db) as conn:
        reasons = [r[0] for r in conn.execute(
            "SELECT reason FROM crawl_errors WHERE run_id = ?",
            (result.run_id,),
        )]
    assert any("Permission" in r or "permission" in r for r in reasons)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_symlink_loop_does_not_infinite_loop(tmp_path: Path):
    root = tmp_path / "share"
    root.mkdir()
    sub = root / "a"
    sub.mkdir()
    (sub / "x.txt").write_text("hi\n")
    # Create a loop: a/loop -> a (its parent)
    os.symlink(sub, sub / "loop")

    walker = LocalScandirWalker(root, follow_symlinks=False)
    items = list(walker.walk())
    # By default symlinks are skipped — should record one symlink-skipped error.
    skipped = [i for i in items if isinstance(i, WalkError)
               and i.reason == "symlink-skipped"]
    assert len(skipped) == 1


def test_excludes_filter_files(tmp_path: Path):
    root = tmp_path / "share"
    root.mkdir()
    (root / "keep.txt").write_text("k\n")
    (root / "skip.tmp").write_text("s\n")
    db = tmp_path / "i.sqlite"
    result = run_crawl(root, db,
                       CrawlOptions(workers=1, exclude_globs=("*.tmp",)))
    assert result.file_count == 1
    with connect(db) as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM files WHERE run_id = ?", (result.run_id,))}
    assert names == {"keep.txt"}


def test_hash_cap_skips_large_files(tmp_path: Path):
    root = tmp_path / "share"
    root.mkdir()
    big = root / "big.bin"
    big.write_bytes(b"x" * (2 * 1024 * 1024))  # 2 MiB
    small = root / "small.txt"
    small.write_text("tiny\n")

    db = tmp_path / "i.sqlite"
    result = run_crawl(root, db,
                       CrawlOptions(workers=1, hash_cap_bytes=1024 * 1024))
    with connect(db) as conn:
        rows = dict(conn.execute(
            "SELECT name, sha256 FROM files WHERE run_id = ?",
            (result.run_id,),
        ).fetchall())
    assert rows["big.bin"] is None
    assert rows["small.txt"] is not None
