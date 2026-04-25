"""End-to-end smoke test of the CLI surface."""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from share_analyzer.cli import main


def test_scan_then_info_then_report(fixture_share: Path, tmp_path: Path):
    db = tmp_path / "idx.sqlite"
    out = tmp_path / "out"
    runner = CliRunner()

    res = runner.invoke(main, ["scan", str(fixture_share), "--db", str(db),
                                "--workers", "2"])
    assert res.exit_code == 0, res.output

    res = runner.invoke(main, ["info", "--db", str(db)])
    assert res.exit_code == 0
    assert "files" in res.output

    res = runner.invoke(main, ["report", "all", "--db", str(db),
                                "--out", str(out)])
    assert res.exit_code == 0
    # At least one .html and one .csv produced
    assert any(p.suffix == ".html" for p in out.rglob("*"))
    assert any(p.suffix == ".csv" for p in out.rglob("*"))
    assert any(p.suffix == ".jsonl" for p in out.rglob("*"))
