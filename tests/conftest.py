"""Shared pytest fixtures: a fixture share with files for crawling and reporting."""
from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from share_analyzer.crawl.orchestrator import CrawlOptions, run_crawl


def _set_mtime(path: Path, days_ago: int) -> None:
    ts = (datetime.now() - timedelta(days=days_ago)).timestamp()
    os.utime(path, (ts, ts))


@pytest.fixture
def fixture_share(tmp_path: Path) -> Path:
    """Build a small fixture share that exercises:
       - nested folders, unicode names, long-ish paths
       - a duplicate cluster (3 copies of the same content)
       - varied MIME categories (text, pdf-ish, image-ish, archive, executable)
       - varied mtimes spanning the staleness buckets
    """
    root = tmp_path / "share"
    root.mkdir()

    docs = root / "Projects" / "alpha" / "docs"
    docs.mkdir(parents=True)
    notes = docs / "notes.md"
    notes.write_text("# Hello\nA short markdown note.\n", encoding="utf-8")
    _set_mtime(notes, 30)

    (docs / "report.txt").write_text("Quarterly numbers go here.\n",
                                       encoding="utf-8")

    # Duplicate cluster: same content, three locations
    dup_content = b"DUPLICATE PAYLOAD " * 200
    for sub in ("a/copy1.txt", "b/copy2.txt", "c/copy3.txt"):
        p = root / "Projects" / "alpha" / "dups" / sub
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(dup_content)
        _set_mtime(p, 100)

    # Stale 5y+ folder
    stale = root / "Archive" / "2018"
    stale.mkdir(parents=True)
    sf = stale / "old_plan.txt"
    sf.write_text("Ancient planning doc.\n", encoding="utf-8")
    _set_mtime(sf, 365 * 6)

    # Unicode-named file
    uni = root / "Projects" / "ünicode"
    uni.mkdir()
    (uni / "über_füße.md").write_text("Umlauts work.\n", encoding="utf-8")

    # Long-ish path (don't go full Windows MAX_PATH on Linux to keep it portable)
    long_dir = root / "Projects" / "deep"
    for i in range(8):
        long_dir = long_dir / f"level_{i:02d}_{'x' * 16}"
    long_dir.mkdir(parents=True)
    (long_dir / "deep_file.txt").write_text("Bottom of the well.\n",
                                              encoding="utf-8")

    # "Archive"-category file
    (root / "Projects" / "alpha" / "release.zip").write_bytes(
        b"PK\x03\x04" + b"\x00" * 64
    )

    # "Executable"-category file
    (root / "Projects" / "alpha" / "tool.exe").write_bytes(
        b"MZ" + b"\x00" * 128
    )

    return root


@pytest.fixture
def crawled_db(fixture_share: Path, tmp_path: Path) -> tuple[Path, int]:
    db = tmp_path / "index.sqlite"
    result = run_crawl(
        fixture_share, db,
        CrawlOptions(workers=2, hash_cap_bytes=10 * 1024 * 1024),
    )
    return db, result.run_id
