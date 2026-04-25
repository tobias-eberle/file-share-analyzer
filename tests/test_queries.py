"""Pure-SQL aggregations: staleness, top_duplicates, rag_candidates."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from share_analyzer.crawl.orchestrator import CrawlOptions, run_crawl
from share_analyzer.index.queries import (
    rag_candidates, staleness_buckets, top_duplicates,
)
from share_analyzer.index.schema import connect


def _set_mtime(p: Path, days_ago: int) -> None:
    ts = (datetime.now() - timedelta(days=days_ago)).timestamp()
    os.utime(p, (ts, ts))


def test_staleness_buckets_pure_sql(tmp_path: Path):
    root = tmp_path / "share"
    root.mkdir()
    fresh = root / "fresh.txt";  fresh.write_text("a"); _set_mtime(fresh, 30)
    one_y = root / "oneyear.txt"; one_y.write_text("b"); _set_mtime(one_y, 600)
    three = root / "threey.txt"; three.write_text("c"); _set_mtime(three, 365 * 4)
    five  = root / "fivey.txt";  five.write_text("d"); _set_mtime(five, 365 * 6)

    db = tmp_path / "i.sqlite"
    result = run_crawl(root, db, CrawlOptions(workers=2))
    with connect(db) as conn:
        buckets = {b["bucket"]: b["file_count"]
                   for b in staleness_buckets(conn, result.run_id)}
    assert buckets["<1y"] == 1
    assert buckets["1-3y"] == 1
    assert buckets["3-5y"] == 1
    assert buckets["5y+"] == 1


def test_top_duplicates_returns_sample_paths(tmp_path: Path):
    root = tmp_path / "share"
    root.mkdir()
    payload = b"DUPE PAYLOAD" * 100
    for sub in ("a", "b", "c", "d"):
        p = root / sub / "copy.bin"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(payload)
    (root / "unique.bin").write_bytes(b"unique")

    db = tmp_path / "i.sqlite"
    result = run_crawl(root, db, CrawlOptions(workers=2))
    with connect(db) as conn:
        rows = top_duplicates(conn, result.run_id)
    assert len(rows) == 1
    cluster = rows[0]
    assert cluster["file_count"] == 4
    assert len(cluster["sample_paths"]) <= 5
    assert all(p.endswith("copy.bin") for p in cluster["sample_paths"])
    assert cluster["wasted_bytes"] == 3 * len(payload)


def test_rag_candidates_dedups_in_sql(tmp_path: Path):
    root = tmp_path / "share"
    root.mkdir()
    # Two identical text files (form a duplicate cluster) + one unique.
    payload = "Quarterly summary, all the words.\n" * 50
    (root / "a.md").write_text(payload, encoding="utf-8")
    (root / "b.md").write_text(payload, encoding="utf-8")
    (root / "c.md").write_text("Other content entirely.\n", encoding="utf-8")

    db = tmp_path / "i.sqlite"
    result = run_crawl(root, db, CrawlOptions(workers=2))
    with connect(db) as conn:
        candidates = list(rag_candidates(
            conn, result.run_id,
            min_size=1, max_size=10 * 1024 * 1024,
            max_age_days=365 * 10,
            categories=("text-extractable",),
            include_one_per_dup=True,
        ))
    # Two distinct sha256 buckets → at most two candidates.
    shas = {c["sha256"] for c in candidates}
    assert len(candidates) == len(shas)
    assert len(candidates) == 2


def test_rag_candidates_without_dedup(tmp_path: Path):
    root = tmp_path / "share"
    root.mkdir()
    payload = "Same content twice.\n" * 50
    (root / "a.md").write_text(payload, encoding="utf-8")
    (root / "b.md").write_text(payload, encoding="utf-8")

    db = tmp_path / "i.sqlite"
    result = run_crawl(root, db, CrawlOptions(workers=2))
    with connect(db) as conn:
        candidates = list(rag_candidates(
            conn, result.run_id,
            min_size=1, max_size=10 * 1024 * 1024,
            max_age_days=365 * 10,
            categories=("text-extractable",),
            include_one_per_dup=False,
        ))
    assert len(candidates) == 2
