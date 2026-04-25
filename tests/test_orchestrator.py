"""Orchestrator: writer-thread exceptions surface to the caller."""
from __future__ import annotations

from pathlib import Path

import pytest

from share_analyzer.crawl.fingerprint import Fingerprint
from share_analyzer.crawl.orchestrator import CrawlOptions, run_crawl
from share_analyzer.crawl.sink import SqliteSink
from share_analyzer.crawl.walker import FileEntry, WalkError


class _ExplodingSink(SqliteSink):
    """SqliteSink that raises on the first batch — simulates a disk-full
    or broken-DB scenario.
    """

    def write_files(self, rows):
        # Burn through the underlying buffer first so the writer thread
        # has actually entered the failing code path.
        list(rows)
        raise RuntimeError("simulated sink failure")


def _populate(tmp_path: Path, n: int = 50) -> Path:
    root = tmp_path / "share"
    sub = root / "a"
    sub.mkdir(parents=True)
    for i in range(n):
        (sub / f"f{i:03d}.txt").write_bytes(b"x" * 32)
    return root


def test_writer_exception_propagates(tmp_path: Path) -> None:
    root = _populate(tmp_path)
    db = tmp_path / "i.sqlite"
    sink = _ExplodingSink(db)

    # The crawl must raise — not hang, not silently succeed with 0 files.
    with pytest.raises(RuntimeError, match="simulated sink failure"):
        run_crawl(
            root, db,
            CrawlOptions(workers=2, queue_size=8),
            sink=sink,
        )


def test_writer_exception_marks_run_failed(tmp_path: Path) -> None:
    root = _populate(tmp_path)
    db = tmp_path / "i.sqlite"
    sink = _ExplodingSink(db)
    with pytest.raises(RuntimeError):
        run_crawl(root, db, CrawlOptions(workers=2), sink=sink)

    from share_analyzer.index.schema import connect
    with connect(db) as conn:
        status = conn.execute(
            "SELECT status FROM crawl_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert status == "failed"
