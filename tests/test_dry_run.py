"""--dry-run: counts files / folders / size without writing the index."""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from share_analyzer.cli import main
from share_analyzer.dry_run import dry_run, format_summary


def _build(tmp_path: Path) -> Path:
    root = tmp_path / "share"
    docs = root / "docs"
    docs.mkdir(parents=True)
    (docs / "a.md").write_text("alpha")
    (docs / "b.txt").write_text("beta beta")
    (root / "c.pdf").write_bytes(b"%PDF-1.4\n" + b"x" * 100)
    return root


def test_dry_run_counts_match_filesystem(tmp_path: Path):
    root = _build(tmp_path)
    summary = dry_run(root)
    assert summary.file_count == 3
    assert summary.folder_count == 2  # root + docs
    assert summary.error_count == 0
    # Compute the expected total from the actual files rather than
    # hand-counting bytes (\n endings + magic numbers are easy to flub).
    expected_size = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
    assert summary.total_size == expected_size
    # extensions counter populated
    assert summary.extensions[".md"] == 1
    assert summary.extensions[".txt"] == 1
    assert summary.extensions[".pdf"] == 1


def test_dry_run_summary_caps_samples(tmp_path: Path):
    root = tmp_path / "share"
    root.mkdir()
    for i in range(20):
        (root / f"f{i:02d}.txt").write_text(str(i))
    summary = dry_run(root)
    assert summary.file_count == 20
    assert len(summary.sample_paths) == 5  # _SAMPLE_LIMIT


def test_format_summary_renders_human_size(tmp_path: Path):
    root = _build(tmp_path)
    summary = dry_run(root)
    out = format_summary(summary)
    assert "[dry-run]" in out
    assert "files" in out
    assert "top" in out
    # contains a unit string
    assert any(unit in out for unit in ("B", "KB", "MB"))


def test_dry_run_does_not_create_db(tmp_path: Path):
    """Sanity: dry-run must not touch SQLite anywhere."""
    root = _build(tmp_path)
    dry_run(root)
    # The dry_run path doesn't take a db argument, so anything sqlite-y
    # would have to land in cwd. Verify nothing did.
    assert not list(tmp_path.glob("*.sqlite*"))


def test_cli_dry_run_smoke(tmp_path: Path):
    root = _build(tmp_path)
    runner = CliRunner()
    res = runner.invoke(main, ["scan", str(root), "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "[dry-run]" in res.output
    assert "files" in res.output
    # No db file created.
    assert not list(tmp_path.glob("*.sqlite*"))


def test_cli_scan_without_db_or_dry_run_errors(tmp_path: Path):
    root = _build(tmp_path)
    runner = CliRunner()
    res = runner.invoke(main, ["scan", str(root)])
    assert res.exit_code != 0
    assert "--db" in res.output


def test_cli_dry_run_uses_default_excludes(tmp_path: Path):
    root = tmp_path / "share"
    root.mkdir()
    (root / "real.txt").write_text("real")
    (root / "Thumbs.db").write_bytes(b"\x00" * 16)
    (root / "~$lock.docx").write_text("lock")

    runner = CliRunner()
    res = runner.invoke(main, ["scan", str(root), "--dry-run"])
    assert res.exit_code == 0, res.output
    # 3 real entries on disk, but Thumbs.db + ~$lock.docx are excluded.
    assert "files       : 1" in res.output
