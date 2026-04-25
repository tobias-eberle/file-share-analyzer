"""Incremental rescan: state classification + deleted-row carryover."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from share_analyzer.crawl.orchestrator import CrawlOptions, run_crawl
from share_analyzer.index.queries import changed_files_summary
from share_analyzer.index.schema import connect


def _build(tmp_path: Path) -> Path:
    root = tmp_path / "share"
    sub = root / "docs"
    sub.mkdir(parents=True)
    (sub / "alpha.txt").write_text("alpha\n", encoding="utf-8")
    (sub / "beta.txt").write_text("beta\n", encoding="utf-8")
    (sub / "gamma.txt").write_text("gamma\n", encoding="utf-8")
    return root


def _states(db: Path, run_id: int) -> dict[str, set[str]]:
    """Return {state: {file paths}} for a run."""
    with connect(db) as conn:
        out: dict[str, set[str]] = {}
        for r in conn.execute(
            "SELECT state, path FROM files WHERE run_id = ?", (run_id,)
        ):
            out.setdefault(r["state"], set()).add(r["path"])
    return out


def test_rescan_with_no_changes_marks_all_unchanged(tmp_path: Path):
    root = _build(tmp_path)
    db = tmp_path / "i.sqlite"
    base = run_crawl(root, db, CrawlOptions(workers=2))

    rescan = run_crawl(
        root, db,
        CrawlOptions(workers=2, previous_run_id=base.run_id),
    )

    sc = rescan.state_counts
    assert sc is not None
    assert sc["unchanged"] == 3
    assert sc["added"] == 0
    assert sc["modified"] == 0
    assert sc["deleted"] == 0


def test_rescan_detects_added_file(tmp_path: Path):
    root = _build(tmp_path)
    db = tmp_path / "i.sqlite"
    base = run_crawl(root, db, CrawlOptions(workers=2))

    (root / "docs" / "delta.txt").write_text("delta\n", encoding="utf-8")
    rescan = run_crawl(
        root, db, CrawlOptions(workers=2, previous_run_id=base.run_id),
    )

    sc = rescan.state_counts
    assert sc["added"] == 1
    assert sc["unchanged"] == 3
    states = _states(db, rescan.run_id)
    assert any(p.endswith("delta.txt") for p in states["added"])


def test_rescan_detects_modified_file(tmp_path: Path):
    root = _build(tmp_path)
    db = tmp_path / "i.sqlite"
    base = run_crawl(root, db, CrawlOptions(workers=2))

    target = root / "docs" / "alpha.txt"
    # Force a different size + later mtime so the (size, mtime) shortcut
    # cannot fire — sleep avoids 1-second mtime resolution collisions.
    time.sleep(1.1)
    target.write_text("alpha v2 with more content\n", encoding="utf-8")

    rescan = run_crawl(
        root, db, CrawlOptions(workers=2, previous_run_id=base.run_id),
    )

    sc = rescan.state_counts
    assert sc["modified"] == 1
    assert sc["unchanged"] == 2
    assert sc["added"] == 0

    # The modified file's sha256 must reflect the new content, not the old.
    with connect(db) as conn:
        old_sha = conn.execute(
            "SELECT sha256 FROM files WHERE run_id = ? AND path = ?",
            (base.run_id, str(target)),
        ).fetchone()[0]
        new_sha = conn.execute(
            "SELECT sha256 FROM files WHERE run_id = ? AND path = ?",
            (rescan.run_id, str(target)),
        ).fetchone()[0]
    assert old_sha is not None and new_sha is not None
    assert old_sha != new_sha


def test_rescan_records_deleted_file(tmp_path: Path):
    root = _build(tmp_path)
    db = tmp_path / "i.sqlite"
    base = run_crawl(root, db, CrawlOptions(workers=2))

    deleted_path = root / "docs" / "beta.txt"
    deleted_path.unlink()
    rescan = run_crawl(
        root, db, CrawlOptions(workers=2, previous_run_id=base.run_id),
    )

    sc = rescan.state_counts
    assert sc["deleted"] == 1
    assert sc["unchanged"] == 2
    states = _states(db, rescan.run_id)
    assert str(deleted_path) in states["deleted"]


def test_rescan_unchanged_reuses_prior_sha256_without_rehash(tmp_path: Path):
    """A FailingFingerprinter would raise if the unchanged path tried to
    rehash — proves the short-circuit is honoured."""
    from share_analyzer.crawl.fingerprint import Fingerprint, Fingerprinter

    class _Forbidden:
        def fingerprint(self, entry):
            raise AssertionError(f"unexpected re-fingerprint of {entry.path}")

    root = _build(tmp_path)
    db = tmp_path / "i.sqlite"
    base = run_crawl(root, db, CrawlOptions(workers=2))

    rescan = run_crawl(
        root, db,
        CrawlOptions(workers=2, previous_run_id=base.run_id),
        fingerprinter=_Forbidden(),
    )
    assert rescan.state_counts["unchanged"] == 3


def test_deleted_rows_excluded_from_reports(tmp_path: Path):
    """materialize_folders + queries must filter out state='deleted'."""
    root = _build(tmp_path)
    db = tmp_path / "i.sqlite"
    base = run_crawl(root, db, CrawlOptions(workers=2))

    (root / "docs" / "beta.txt").unlink()
    rescan = run_crawl(
        root, db, CrawlOptions(workers=2, previous_run_id=base.run_id),
    )

    with connect(db) as conn:
        # folders aggregate must reflect the live snapshot, not include
        # the deleted file's bytes.
        size = conn.execute(
            "SELECT total_size FROM folders WHERE run_id = ? AND path = ?",
            (rescan.run_id, str(root)),
        ).fetchone()[0]
        live_size = conn.execute(
            "SELECT SUM(size) FROM files WHERE run_id = ? AND state != 'deleted'",
            (rescan.run_id,),
        ).fetchone()[0]
    assert size == live_size

    # And the deleted file isn't counted in any per-state sum that matters
    # to consumers — the summary must put it in the deleted bucket.
    with connect(db) as conn:
        sc = changed_files_summary(conn, rescan.run_id)
    assert sc["deleted"] == 1
