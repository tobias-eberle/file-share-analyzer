"""Files bucketed by last-modified age."""
from __future__ import annotations

from pathlib import Path

import plotly.graph_objects as go

from share_analyzer.index.queries import run_summary, staleness_buckets
from share_analyzer.reports.base import (
    ReportArtifact, register, render_plotly, write_csv, write_html,
)


@register("staleness", "Files by latest-mtime bucket", ("html", "csv"))
def report_staleness(*, conn, run_id, out_dir: Path, formats):
    buckets = staleness_buckets(conn, run_id)
    artifact = ReportArtifact(name="staleness", paths=[])

    if "csv" in formats:
        artifact.paths.append(write_csv(out_dir / "staleness.csv", buckets))

    if "html" in formats:
        chart_html = ""
        if any(b["file_count"] for b in buckets):
            fig = go.Figure(go.Bar(
                x=[b["bucket"] for b in buckets],
                y=[b["total_size"] for b in buckets],
            ))
            fig.update_layout(
                title="Total size by mtime bucket",
                xaxis_title="Age bucket",
                yaxis_title="Total size (bytes)",
                margin=dict(t=40, l=10, r=10, b=10),
            )
            chart_html = render_plotly(fig)
        artifact.paths.append(write_html(
            out_dir / "staleness.html",
            "staleness.html.j2",
            {
                "title": "Staleness",
                "run": run_summary(conn, run_id),
                "buckets": buckets,
                "chart": chart_html,
            },
        ))
    return artifact
