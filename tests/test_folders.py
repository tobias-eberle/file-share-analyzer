"""materialize_folders: ancestor synthesis, recursive rollup, max_depth_below."""
from __future__ import annotations

from pathlib import Path

from share_analyzer.crawl.orchestrator import CrawlOptions, run_crawl
from share_analyzer.index.schema import connect


def _build_share(tmp_path: Path) -> Path:
    """A share where the only files live three levels deep, so every
    intermediate folder is a 'no direct files' ancestor.
    """
    root = tmp_path / "share"
    deep = root / "L1" / "L2" / "L3"
    deep.mkdir(parents=True)
    (deep / "a.txt").write_bytes(b"AAAA")          # 4 bytes
    (deep / "b.txt").write_bytes(b"BBBBBBBB")      # 8 bytes
    sibling = root / "L1" / "L2" / "Sibling"
    sibling.mkdir()
    (sibling / "c.txt").write_bytes(b"CC")         # 2 bytes
    return root


def test_ancestors_are_synthesized(tmp_path: Path) -> None:
    root = _build_share(tmp_path)
    db = tmp_path / "i.sqlite"
    result = run_crawl(root, db, CrawlOptions(workers=2))
    with connect(db) as conn:
        paths = {r[0] for r in conn.execute(
            "SELECT path FROM folders WHERE run_id = ?", (result.run_id,)
        )}
    # Intermediate "L1" and "L1/L2" hold no direct files — they must
    # still appear so the topology treemap has parent nodes.
    assert str(root) in paths
    assert str(root / "L1") in paths
    assert str(root / "L1" / "L2") in paths
    assert str(root / "L1" / "L2" / "L3") in paths
    assert str(root / "L1" / "L2" / "Sibling") in paths


def test_total_size_is_recursive(tmp_path: Path) -> None:
    root = _build_share(tmp_path)
    db = tmp_path / "i.sqlite"
    result = run_crawl(root, db, CrawlOptions(workers=2))
    with connect(db) as conn:
        sizes = dict(conn.execute(
            "SELECT path, total_size FROM folders WHERE run_id = ?",
            (result.run_id,),
        ).fetchall())
    # All 14 bytes (4 + 8 + 2) must roll up to root.
    assert sizes[str(root)] == 14
    assert sizes[str(root / "L1")] == 14
    assert sizes[str(root / "L1" / "L2")] == 14
    # The L3 leaf holds 4+8 directly.
    assert sizes[str(root / "L1" / "L2" / "L3")] == 12
    assert sizes[str(root / "L1" / "L2" / "Sibling")] == 2


def test_file_count_is_recursive(tmp_path: Path) -> None:
    root = _build_share(tmp_path)
    db = tmp_path / "i.sqlite"
    result = run_crawl(root, db, CrawlOptions(workers=2))
    with connect(db) as conn:
        counts = dict(conn.execute(
            "SELECT path, file_count FROM folders WHERE run_id = ?",
            (result.run_id,),
        ).fetchall())
    assert counts[str(root)] == 3
    assert counts[str(root / "L1" / "L2" / "L3")] == 2
    assert counts[str(root / "L1" / "L2" / "Sibling")] == 1


def test_max_depth_below_propagates(tmp_path: Path) -> None:
    root = _build_share(tmp_path)
    db = tmp_path / "i.sqlite"
    result = run_crawl(root, db, CrawlOptions(workers=2))
    with connect(db) as conn:
        mdb = dict(conn.execute(
            "SELECT path, max_depth_below FROM folders WHERE run_id = ?",
            (result.run_id,),
        ).fetchall())
    # root → L1 → L2 → L3   = 3 levels deep
    assert mdb[str(root)] == 3
    assert mdb[str(root / "L1")] == 2
    assert mdb[str(root / "L1" / "L2")] == 1
    # Leaf has no descendants.
    assert mdb[str(root / "L1" / "L2" / "L3")] == 0
