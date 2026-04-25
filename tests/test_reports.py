"""Report generation against the fixture share."""
from __future__ import annotations

import json
from pathlib import Path

from share_analyzer.index.schema import connect
from share_analyzer.reports import REPORTS, run_all, run_report


def test_all_reports_run(crawled_db, tmp_path: Path):
    db, run_id = crawled_db
    out = tmp_path / "reports"
    with connect(db) as conn:
        artifacts = run_all(conn, run_id, out)

    by_name = {a.name: a for a in artifacts}
    assert set(by_name) == set(REPORTS)
    for art in artifacts:
        for p in art.paths:
            assert p.exists() and p.stat().st_size > 0


def test_html_reports_are_self_contained(crawled_db, tmp_path: Path):
    db, run_id = crawled_db
    out = tmp_path / "reports"
    with connect(db) as conn:
        run_all(conn, run_id, out)

    html_files = list(out.rglob("*.html"))
    assert html_files
    for hf in html_files:
        body = hf.read_text(encoding="utf-8")
        assert "<title>" in body
        # No CDN <script src=> tags — Plotly JS is inlined.
        assert 'src="https://cdn.' not in body
        assert "src='https://cdn." not in body


def test_csv_topology_has_paths(crawled_db, tmp_path: Path):
    db, run_id = crawled_db
    out = tmp_path / "reports"
    with connect(db) as conn:
        run_report("topology", conn, run_id, out, "csv")
    text = (out / "topology.csv").read_text(encoding="utf-8")
    assert "path" in text.splitlines()[0]
    assert text.count("\n") > 1


def test_rag_candidates_jsonl_is_valid(crawled_db, tmp_path: Path):
    db, run_id = crawled_db
    out = tmp_path / "reports"
    with connect(db) as conn:
        run_report("rag_candidates", conn, run_id, out, "all")
    lines = (out / "rag_candidates.jsonl").read_text(encoding="utf-8").splitlines()
    assert lines, "expected at least one RAG candidate from the fixture share"
    parsed = [json.loads(l) for l in lines]
    for row in parsed:
        assert {"path", "size", "mtime", "sha256",
                "mime_type", "mime_category", "suggested_category"} <= row.keys()
        assert row["mime_category"] == "text-extractable"


def test_duplication_report_lists_cluster(crawled_db, tmp_path: Path):
    db, run_id = crawled_db
    out = tmp_path / "reports"
    with connect(db) as conn:
        run_report("duplication", conn, run_id, out, "all")
    csv_text = (out / "duplication.csv").read_text(encoding="utf-8")
    assert "wasted_bytes" in csv_text.splitlines()[0]
    # Three-copy cluster from the fixture should show file_count=3.
    assert ",3," in csv_text


def test_staleness_buckets_reach_5y_plus(crawled_db, tmp_path: Path):
    db, run_id = crawled_db
    out = tmp_path / "reports"
    with connect(db) as conn:
        run_report("staleness", conn, run_id, out, "all")
    text = (out / "staleness.csv").read_text(encoding="utf-8")
    # Find the 5y+ row and ensure non-zero count.
    for line in text.splitlines()[1:]:
        if line.startswith("5y+"):
            _, count, _ = line.split(",", 2)
            assert int(count) >= 1
            break
    else:
        raise AssertionError("5y+ bucket missing from staleness report")
