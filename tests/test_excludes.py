"""Default-exclude list and CLI/TOML composition."""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from share_analyzer.cli import _resolve_excludes, main
from share_analyzer.config import DEFAULT_EXCLUDES
from share_analyzer.crawl.orchestrator import CrawlOptions, run_crawl
from share_analyzer.crawl.walker import LocalScandirWalker
from share_analyzer.index.schema import connect


def _share_with_nuisance_files(tmp_path: Path) -> Path:
    root = tmp_path / "share"
    docs = root / "docs"
    docs.mkdir(parents=True)
    (docs / "report.docx").write_text("real document")
    (docs / "~$report.docx").write_text("Office lock file")
    (root / "Thumbs.db").write_bytes(b"\x00" * 16)
    (root / "desktop.ini").write_text("[.ShellClassInfo]")
    (root / "build.tmp").write_text("temp")
    return root


def test_default_excludes_filter_nuisance_files(tmp_path: Path):
    root = _share_with_nuisance_files(tmp_path)
    db = tmp_path / "i.sqlite"
    result = run_crawl(
        root, db,
        CrawlOptions(workers=2, exclude_globs=DEFAULT_EXCLUDES),
    )
    with connect(db) as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM files WHERE run_id = ?", (result.run_id,)
        )}
    assert names == {"report.docx"}


def test_no_default_excludes_includes_everything(tmp_path: Path):
    root = _share_with_nuisance_files(tmp_path)
    db = tmp_path / "i.sqlite"
    # Empty exclude list = include all.
    result = run_crawl(root, db, CrawlOptions(workers=2, exclude_globs=()))
    with connect(db) as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM files WHERE run_id = ?", (result.run_id,)
        )}
    # All five files visible without the curated list.
    assert {"report.docx", "~$report.docx", "Thumbs.db",
            "desktop.ini", "build.tmp"} <= names


def test_resolve_excludes_composes_defaults_then_cfg_then_cli():
    cfg = {"exclude": ["*.bak"]}
    cli = ("*.swp",)
    result = _resolve_excludes(cfg, cli, no_defaults=False)
    # Defaults first, cfg next, CLI last; all present.
    assert result[: len(DEFAULT_EXCLUDES)] == DEFAULT_EXCLUDES
    assert "*.bak" in result
    assert "*.swp" in result


def test_resolve_excludes_no_defaults_via_cli_flag():
    result = _resolve_excludes({}, ("foo",), no_defaults=True)
    assert result == ("foo",)


def test_resolve_excludes_no_defaults_via_toml():
    result = _resolve_excludes({"default_excludes": False}, ("foo",),
                                no_defaults=False)
    assert result == ("foo",)


def test_cli_scan_applies_default_excludes(tmp_path: Path):
    """End-to-end: `share-analyzer scan` without --no-default-excludes
    must skip Thumbs.db, ~$lock.docx, etc.
    """
    root = _share_with_nuisance_files(tmp_path)
    db = tmp_path / "i.sqlite"
    runner = CliRunner()
    res = runner.invoke(main, ["scan", str(root), "--db", str(db),
                                "--workers", "2"])
    assert res.exit_code == 0, res.output
    with connect(db) as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM files"
        )}
    assert names == {"report.docx"}


def test_cli_scan_no_default_excludes_flag_includes_all(tmp_path: Path):
    root = _share_with_nuisance_files(tmp_path)
    db = tmp_path / "i.sqlite"
    runner = CliRunner()
    res = runner.invoke(main, [
        "scan", str(root), "--db", str(db),
        "--workers", "2", "--no-default-excludes",
    ])
    assert res.exit_code == 0, res.output
    with connect(db) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM files")}
    assert "Thumbs.db" in names
    assert "~$report.docx" in names
