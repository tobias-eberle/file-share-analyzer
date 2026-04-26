"""Parallel directory enumeration via dir_workers > 1."""
from __future__ import annotations

from pathlib import Path

from share_analyzer.crawl.orchestrator import CrawlOptions, run_crawl
from share_analyzer.crawl.walker import (
    FileEntry, LocalScandirWalker, WalkError,
)
from share_analyzer.index.schema import connect


def _build_branchy(tmp_path: Path, branches: int = 12,
                   files_per_dir: int = 4) -> Path:
    """Wide tree so the parallel walker actually has work to spread."""
    root = tmp_path / "share"
    root.mkdir()
    for b in range(branches):
        sub = root / f"branch_{b:02d}"
        sub.mkdir()
        for f in range(files_per_dir):
            (sub / f"file_{f:02d}.txt").write_text(f"branch={b} file={f}\n")
        # nested level so the worker pool actually trades work
        nested = sub / "nested"
        nested.mkdir()
        for f in range(files_per_dir):
            (nested / f"deep_{f:02d}.txt").write_text(f"deep {b}/{f}\n")
    return root


def test_parallel_walker_finds_every_file(tmp_path: Path):
    root = _build_branchy(tmp_path)
    seq = LocalScandirWalker(root, dir_workers=1)
    par = LocalScandirWalker(root, dir_workers=4)

    seq_files = sorted(i.path for i in seq.walk() if isinstance(i, FileEntry))
    par_files = sorted(i.path for i in par.walk() if isinstance(i, FileEntry))
    assert seq_files == par_files
    assert len(seq_files) == 12 * 4 * 2  # branches × files × (root + nested)


def test_parallel_walker_emits_walk_errors(tmp_path: Path):
    """A non-existent root must surface as one WalkError, not a hang."""
    bogus = tmp_path / "does-not-exist"
    par = LocalScandirWalker(bogus, dir_workers=4)
    items = list(par.walk())
    errors = [i for i in items if isinstance(i, WalkError)]
    assert len(errors) == 1
    assert errors[0].path == str(bogus)


def test_parallel_walker_respects_excludes(tmp_path: Path):
    root = _build_branchy(tmp_path, branches=4, files_per_dir=2)
    par = LocalScandirWalker(root, dir_workers=3, exclude_globs=("*deep_00*",))
    files = [i for i in par.walk() if isinstance(i, FileEntry)]
    # 4 branches × 2 levels × 2 files = 16; excludes drop 4 deep_00.txt
    # one per branch in `nested/` = 4 excluded.
    assert len(files) == 16 - 4
    assert not any("deep_00" in f.path for f in files)


def test_parallel_walker_terminates_on_empty_root(tmp_path: Path):
    """Root that exists but is empty must terminate cleanly."""
    root = tmp_path / "empty"
    root.mkdir()
    par = LocalScandirWalker(root, dir_workers=4)
    items = list(par.walk())
    assert items == []


def test_parallel_walker_via_orchestrator(tmp_path: Path):
    """End-to-end: a scan with dir_workers=4 indexes the same files as dir_workers=1."""
    root = _build_branchy(tmp_path, branches=6, files_per_dir=3)

    db_seq = tmp_path / "seq.sqlite"
    res_seq = run_crawl(root, db_seq, CrawlOptions(workers=2, dir_workers=1))

    db_par = tmp_path / "par.sqlite"
    res_par = run_crawl(root, db_par, CrawlOptions(workers=2, dir_workers=4))

    assert res_seq.file_count == res_par.file_count

    def _files(db: Path, run_id: int) -> set[str]:
        with connect(db) as conn:
            return {r[0] for r in conn.execute(
                "SELECT path FROM files WHERE run_id = ?", (run_id,)
            )}
    assert _files(db_seq, res_seq.run_id) == _files(db_par, res_par.run_id)
