"""Unified dashboard report — structure + self-containment + KPIs."""
from __future__ import annotations

import re
from pathlib import Path

from share_analyzer.index.schema import connect
from share_analyzer.reports import REPORTS, run_all, run_report


def test_dashboard_is_a_registered_report():
    assert "dashboard" in REPORTS
    spec = REPORTS["dashboard"]
    assert spec.formats == ("html",)


def test_dashboard_html_emitted_by_run_all(crawled_db, tmp_path: Path):
    db, run_id = crawled_db
    out = tmp_path / "reports"
    with connect(db) as conn:
        run_all(conn, run_id, out)
    assert (out / "dashboard.html").exists()


def test_dashboard_is_the_only_html(crawled_db, tmp_path: Path):
    """The whole point of the refactor — one HTML, not five."""
    db, run_id = crawled_db
    out = tmp_path / "reports"
    with connect(db) as conn:
        run_all(conn, run_id, out)
    htmls = sorted(p.name for p in out.glob("*.html"))
    assert htmls == ["dashboard.html"]


def test_csvs_and_jsonl_still_produced(crawled_db, tmp_path: Path):
    db, run_id = crawled_db
    out = tmp_path / "reports"
    with connect(db) as conn:
        run_all(conn, run_id, out)
    expected_csvs = {
        "topology.csv",
        "size_hotspots.csv",
        "staleness.csv",
        "duplication.csv",
        "type_distribution_categories.csv",
        "type_distribution_extensions.csv",
    }
    actual = {p.name for p in out.iterdir()}
    assert expected_csvs <= actual
    assert "rag_candidates.jsonl" in actual


def test_dashboard_is_self_contained(crawled_db, tmp_path: Path):
    """Plotly inlined, no CDN script tags, no <link href="http…"."""
    db, run_id = crawled_db
    out = tmp_path / "reports"
    with connect(db) as conn:
        run_report("dashboard", conn, run_id, out, "all")
    body = (out / "dashboard.html").read_text(encoding="utf-8")
    assert 'src="https://cdn.' not in body
    assert "src='https://cdn." not in body
    assert '<link href="http' not in body


def test_dashboard_inlines_plotly_only_once(crawled_db, tmp_path: Path):
    """Each `to_html(include_plotlyjs='inline')` injects the full
    bundle. With seven figures we'd weigh ~25 MB if we let every chart
    inline; the dashboard should embed the JS once."""
    db, run_id = crawled_db
    out = tmp_path / "reports"
    with connect(db) as conn:
        run_report("dashboard", conn, run_id, out, "all")
    body = (out / "dashboard.html").read_text(encoding="utf-8")
    # Plotly's bundle starts with a recognisable script-tag wrapper
    # containing `Plotly` as a global. Counting bundle markers gives
    # us the number of full inlines.
    matches = re.findall(r"/\*\* @license MIT plotly", body)
    assert len(matches) <= 1, (
        f"expected Plotly inlined at most once, found {len(matches)}"
    )


def test_dashboard_contains_all_section_headings(crawled_db, tmp_path: Path):
    db, run_id = crawled_db
    out = tmp_path / "reports"
    with connect(db) as conn:
        run_report("dashboard", conn, run_id, out, "all")
    body = (out / "dashboard.html").read_text(encoding="utf-8")
    for heading in (
        "Topology",
        "Size hotspots",
        "Staleness",
        "Duplication",
        "Types",
        "RAG hand-off",
    ):
        assert f">{heading}<" in body, f"missing section: {heading}"


def test_dashboard_kpi_strip_has_every_key(crawled_db, tmp_path: Path):
    db, run_id = crawled_db
    out = tmp_path / "reports"
    with connect(db) as conn:
        run_report("dashboard", conn, run_id, out, "all")
    body = (out / "dashboard.html").read_text(encoding="utf-8")
    for label in ("Files", "Total size", "Stale (5y+)",
                   "Duplication", "RAG-ready"):
        assert label in body


def test_dashboard_renders_run_metadata(crawled_db, tmp_path: Path):
    db, run_id = crawled_db
    out = tmp_path / "reports"
    with connect(db) as conn:
        run_report("dashboard", conn, run_id, out, "all")
    body = (out / "dashboard.html").read_text(encoding="utf-8")
    # Run id pill + status badge.
    assert f"Run #{run_id}" in body
    assert ">complete<" in body  # status pill — fixture run completes


def test_dashboard_chart_empty_states(tmp_path: Path):
    """A run with zero rows still renders cleanly — no Jinja crash, and
    every chart slot shows the empty-state fallback."""
    from share_analyzer.crawl.orchestrator import CrawlOptions, run_crawl

    empty = tmp_path / "share"
    empty.mkdir()
    db = tmp_path / "i.sqlite"
    result = run_crawl(empty, db, CrawlOptions(workers=1))

    out = tmp_path / "reports"
    with connect(db) as conn:
        run_report("dashboard", conn, result.run_id, out, "all")
    body = (out / "dashboard.html").read_text(encoding="utf-8")
    # The empty-state copy lives in the template; at least one chart
    # slot fell back to it.
    assert "chart-empty" in body


def test_dashboard_renders_kpi_total_bytes_correctly(crawled_db, tmp_path: Path):
    """Sanity: the KPI's total-size value matches what's in `files`."""
    db, run_id = crawled_db
    out = tmp_path / "reports"
    with connect(db) as conn:
        run_report("dashboard", conn, run_id, out, "all")
        actual_total = conn.execute(
            "SELECT SUM(size) FROM files WHERE run_id = ? AND state != 'deleted'",
            (run_id,),
        ).fetchone()[0]
    body = (out / "dashboard.html").read_text(encoding="utf-8")
    # The KPI strip includes `{:,}` of the raw byte total. Look for
    # that exact comma-grouped figure.
    assert f"{actual_total:,} bytes" in body
