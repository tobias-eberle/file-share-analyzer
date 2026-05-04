"""Unified dashboard report — one self-contained HTML with the whole story.

Replaces the five-separate-pages HTML output. Inlines Plotly *once*
(then `include_plotlyjs=False` for every subsequent figure) so the
output isn't N × the JS bundle. Theme is applied centrally so all
charts share fonts, colours, and chrome.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import plotly.graph_objects as go

from share_analyzer.index.queries import (
    category_distribution, extension_distribution, run_summary,
    size_hotspots, staleness_buckets, topology, top_duplicates,
    rag_candidates,
)
from share_analyzer.reports.base import (
    ReportArtifact, register, write_html,
)


_FONT_FAMILY = (
    '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, '
    '"Helvetica Neue", Arial, sans-serif'
)
_TRANSPARENT = "rgba(0,0,0,0)"
_TEXT_COLOR = "#0f172a"
_GRID_COLOR = "#f1f5f9"
_AXIS_COLOR = "#cbd5e1"

# MIME category → colour. Same palette as the CSS pill tags so the
# treemap, donut, and inline pills all line up.
_MIME_COLORS = {
    "text-extractable": "#2563eb",
    "ocr-needed":       "#d97706",
    "media":            "#db2777",
    "archive":          "#475569",
    "executable":       "#9333ea",
    "other":            "#94a3b8",
}

# Staleness gradient: cool (fresh) → warm (old).
_STALE_COLORS = {
    "<1y":  "#10b981",
    "1-3y": "#84cc16",
    "3-5y": "#f59e0b",
    "5y+":  "#ef4444",
}

_HOTSPOT_COLOR = "#2563eb"
_DUP_COLOR = "#f59e0b"
_EXT_COLOR = "#475569"


def _theme(fig: go.Figure, *, height: int = 320) -> go.Figure:
    """Apply the dashboard's visual language to `fig` in place."""
    fig.update_layout(
        font=dict(family=_FONT_FAMILY, size=12, color=_TEXT_COLOR),
        paper_bgcolor=_TRANSPARENT,
        plot_bgcolor=_TRANSPARENT,
        margin=dict(t=8, l=8, r=8, b=8),
        height=height,
        showlegend=False,
        hoverlabel=dict(font=dict(family=_FONT_FAMILY, size=12)),
    )
    fig.update_xaxes(gridcolor=_GRID_COLOR, linecolor=_AXIS_COLOR,
                      zerolinecolor=_GRID_COLOR, ticks="outside",
                      tickcolor=_AXIS_COLOR)
    fig.update_yaxes(gridcolor=_GRID_COLOR, linecolor=_AXIS_COLOR,
                      zerolinecolor=_GRID_COLOR, ticks="outside",
                      tickcolor=_AXIS_COLOR)
    return fig


def _to_html(fig: go.Figure, *, include_js: bool) -> str:
    """Render a Plotly figure as an inlined snippet.

    `include_js=True` for the FIRST figure on the page only; the
    remaining figures share the global `Plotly` from that load. Saves
    ~3 MB of JS per additional figure.
    """
    return fig.to_html(
        include_plotlyjs="inline" if include_js else False,
        full_html=False,
        config={
            "displaylogo": False,
            "responsive": True,
            "modeBarButtonsToRemove": [
                "select2d", "lasso2d", "autoScale2d",
            ],
        },
    )


def _short(path: str) -> str:
    """Folder name from a full path, with a left-truncation fallback for
    very long single-segment paths (e.g. UNC roots)."""
    name = os.path.basename(path.rstrip("/\\")) or path
    if len(name) > 48:
        name = "…" + name[-47:]
    return name


def _topology_chart(rows: list[dict], include_js: bool) -> str:
    if not rows:
        return ""
    labels = [r["path"] for r in rows]
    parents = [r["parent_path"] or "" for r in rows]
    values = [r["total_size"] or 0 for r in rows]
    colors = [
        _MIME_COLORS.get(r["dominant_mime_category"] or "other", _MIME_COLORS["other"])
        for r in rows
    ]
    fig = go.Figure(go.Treemap(
        labels=labels, parents=parents, values=values,
        branchvalues="total",
        marker=dict(colors=colors, line=dict(color="#ffffff", width=1)),
        textfont=dict(family=_FONT_FAMILY, size=11, color="#ffffff"),
        hovertemplate="<b>%{label}</b><br>%{value:,} bytes<extra></extra>",
        pathbar=dict(visible=True, thickness=20),
    ))
    _theme(fig, height=440)
    return _to_html(fig, include_js=include_js)


def _hotspot_chart(rows: list[dict], include_js: bool) -> str:
    if not rows:
        return ""
    # Reverse so the largest bar sits at the top.
    labels = [_short(r["path"]) for r in rows][::-1]
    values = [r["total_size"] or 0 for r in rows][::-1]
    fig = go.Figure(go.Bar(
        x=values, y=labels, orientation="h",
        marker=dict(color=_HOTSPOT_COLOR),
        hovertemplate="%{y}<br>%{x:,} bytes<extra></extra>",
    ))
    _theme(fig, height=320)
    fig.update_xaxes(title=None, tickformat="~s")
    fig.update_yaxes(title=None, automargin=True)
    return _to_html(fig, include_js=include_js)


def _staleness_chart(buckets: list[dict], include_js: bool) -> str:
    if not any(b["file_count"] for b in buckets):
        return ""
    fig = go.Figure(go.Bar(
        x=[b["bucket"] for b in buckets],
        y=[b["total_size"] or 0 for b in buckets],
        marker=dict(color=[_STALE_COLORS[b["bucket"]] for b in buckets]),
        text=[
            f"{b['file_count']:,} files" for b in buckets
        ],
        textposition="outside",
        textfont=dict(size=11, color="#475569"),
        hovertemplate="<b>%{x}</b><br>%{y:,} bytes<extra></extra>",
    ))
    _theme(fig, height=300)
    fig.update_xaxes(title=None)
    fig.update_yaxes(title=None, tickformat="~s")
    return _to_html(fig, include_js=include_js)


def _dup_chart(rows: list[dict], include_js: bool) -> str:
    if not rows:
        return ""
    labels = [
        f"{r['sha256'][:8]}… ({r['file_count']}×)" for r in rows
    ][::-1]
    values = [r["wasted_bytes"] for r in rows][::-1]
    fig = go.Figure(go.Bar(
        x=values, y=labels, orientation="h",
        marker=dict(color=_DUP_COLOR),
        hovertemplate="%{y}<br>%{x:,} bytes wasted<extra></extra>",
    ))
    _theme(fig, height=340)
    fig.update_xaxes(title=None, tickformat="~s")
    fig.update_yaxes(title=None, automargin=True)
    return _to_html(fig, include_js=include_js)


def _category_chart(cats: list[dict], include_js: bool) -> str:
    if not cats:
        return ""
    fig = go.Figure(go.Pie(
        labels=[c["category"] for c in cats],
        values=[c["total_size"] or 0 for c in cats],
        hole=0.55,
        marker=dict(
            colors=[
                _MIME_COLORS.get(c["category"], _MIME_COLORS["other"])
                for c in cats
            ],
            line=dict(color="#ffffff", width=2),
        ),
        textinfo="label+percent",
        textfont=dict(family=_FONT_FAMILY, size=11),
        hovertemplate="<b>%{label}</b><br>%{value:,} bytes (%{percent})<extra></extra>",
    ))
    _theme(fig, height=320)
    fig.update_layout(showlegend=False)
    return _to_html(fig, include_js=include_js)


def _extension_chart(exts: list[dict], include_js: bool) -> str:
    if not exts:
        return ""
    labels = [e["extension"] for e in exts][::-1]
    values = [e["file_count"] for e in exts][::-1]
    fig = go.Figure(go.Bar(
        x=values, y=labels, orientation="h",
        marker=dict(color=_EXT_COLOR),
        hovertemplate="%{y}<br>%{x:,} files<extra></extra>",
    ))
    _theme(fig, height=340)
    fig.update_xaxes(title=None, tickformat="~s")
    fig.update_yaxes(title=None, automargin=True)
    return _to_html(fig, include_js=include_js)


# Defaults match `reports/rag_candidates.py` so the dashboard's count
# matches the JSONL hand-off.
_RAG_MIN_BYTES = 1024
_RAG_MAX_BYTES = 50 * 1024 * 1024
_RAG_MAX_AGE_DAYS = 365 * 5


def _rag_summary(conn, run_id: int) -> tuple[int, int]:
    """Walk the rag_candidates generator once to count + sum sizes.

    Cheap on tens of thousands of rows; on multi-million-file shares
    a SQL-side aggregate would beat it, but the dashboard runs once
    per scan, not on the hot path.
    """
    n = 0
    total = 0
    for row in rag_candidates(
        conn, run_id,
        min_size=_RAG_MIN_BYTES,
        max_size=_RAG_MAX_BYTES,
        max_age_days=_RAG_MAX_AGE_DAYS,
    ):
        n += 1
        total += row.get("size") or 0
    return n, total


def _total_wasted_and_clusters(conn, run_id: int) -> tuple[int, int]:
    """Sum every duplicate cluster's wasted bytes across the whole run."""
    row = conn.execute(
        """
        WITH dup_clusters AS (
            SELECT sha256, COUNT(*) AS file_count, MAX(size) AS file_size
            FROM files
            WHERE run_id = ? AND state != 'deleted' AND sha256 IS NOT NULL
            GROUP BY sha256
            HAVING file_count >= 2
        )
        SELECT
            COALESCE(SUM((file_count - 1) * file_size), 0) AS wasted,
            COUNT(*)                                         AS clusters
        FROM dup_clusters
        """,
        (run_id,),
    ).fetchone()
    return int(row[0] or 0), int(row[1] or 0)


@register("dashboard",
           "Unified dashboard (single self-contained HTML)",
           ("html",))
def report_dashboard(*, conn, run_id, out_dir: Path,
                      formats) -> ReportArtifact:
    if "html" not in formats:
        return ReportArtifact(name="dashboard", paths=[])

    run = run_summary(conn, run_id)

    # ---- Pull every chart's data up-front --------------------------
    topo_rows = topology(conn, run_id, max_depth=4)
    h1_rows = size_hotspots(conn, run_id, level=1, limit=10)
    h2_rows = size_hotspots(conn, run_id, level=2, limit=10)
    staleness_rows = staleness_buckets(conn, run_id)
    dup_rows = top_duplicates(conn, run_id, limit=10)
    cat_rows = category_distribution(conn, run_id)
    ext_rows = extension_distribution(conn, run_id, limit=15)

    # ---- Aggregates that drive the KPI strip -----------------------
    folder_count = conn.execute(
        "SELECT COUNT(*) FROM folders WHERE run_id = ?", (run_id,),
    ).fetchone()[0]
    total_size = sum((c.get("total_size") or 0) for c in cat_rows)
    files_total = run.get("file_count") or 0
    stale_5y = next(
        (b["file_count"] for b in staleness_rows if b["bucket"] == "5y+"), 0,
    )
    stale_pct = (stale_5y / files_total * 100) if files_total else 0.0
    wasted, dup_clusters = _total_wasted_and_clusters(conn, run_id)
    rag_count, rag_size = _rag_summary(conn, run_id)

    kpis: dict[str, Any] = {
        "files": files_total,
        "size": total_size,
        "folders": folder_count,
        "stale_5y_count": stale_5y,
        "stale_pct": stale_pct,
        "wasted": wasted,
        "dup_clusters": dup_clusters,
        "rag_candidates": rag_count,
        "rag_size": rag_size,
        "errors": run.get("error_count") or 0,
    }

    # ---- Charts ----------------------------------------------------
    # Inline Plotly's JS exactly once; every subsequent chart references
    # the shared global. ~3 MB per saved JS bundle.
    charts: dict[str, str] = {}
    first = [True]

    def render(fn, *args):
        html = fn(*args, include_js=first[0])
        if html and first[0]:
            first[0] = False
        return html

    charts["topology"] = render(_topology_chart, topo_rows)
    charts["h1"] = render(_hotspot_chart, h1_rows)
    charts["h2"] = render(_hotspot_chart, h2_rows)
    charts["staleness"] = render(_staleness_chart, staleness_rows)
    charts["duplication"] = render(_dup_chart, dup_rows)
    charts["category"] = render(_category_chart, cat_rows)
    charts["extension"] = render(_extension_chart, ext_rows)

    out_path = write_html(
        out_dir / "dashboard.html",
        "dashboard.html.j2",
        {
            "title": "Share Analyzer · Dashboard",
            "run": run,
            "kpis": kpis,
            "topology_chart": charts["topology"],
            "topology_rows": topo_rows,
            "h1_chart": charts["h1"],
            "h2_chart": charts["h2"],
            "staleness_chart": charts["staleness"],
            "staleness_rows": staleness_rows,
            "dup_chart": charts["duplication"],
            "dup_rows": dup_rows,
            "cat_chart": charts["category"],
            "ext_chart": charts["extension"],
        },
    )
    return ReportArtifact(name="dashboard", paths=[out_path])
