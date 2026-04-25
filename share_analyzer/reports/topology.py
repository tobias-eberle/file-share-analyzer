"""Folder tree to N levels with treemap visualization."""
from __future__ import annotations

from pathlib import Path

import plotly.graph_objects as go

from share_analyzer.index.queries import run_summary, topology
from share_analyzer.reports.base import (
    ReportArtifact, register, render_plotly, write_csv, write_html,
)


@register("topology", "Folder tree (treemap + table)", ("html", "csv"))
def report_topology(*, conn, run_id, out_dir: Path, formats):
    rows = topology(conn, run_id, max_depth=4)
    artifact = ReportArtifact(name="topology", paths=[])

    if "csv" in formats:
        artifact.paths.append(
            write_csv(out_dir / "topology.csv", rows)
        )

    if "html" in formats:
        chart_html = ""
        if rows:
            labels = [r["path"] for r in rows]
            parents = [r["parent_path"] or "" for r in rows]
            values = [r["total_size"] or 0 for r in rows]
            colors = [r["dominant_mime_category"] or "other" for r in rows]
            fig = go.Figure(go.Treemap(
                labels=labels,
                parents=parents,
                values=values,
                customdata=colors,
                hovertemplate="<b>%{label}</b><br>%{value:,} bytes<br>%{customdata}<extra></extra>",
            ))
            fig.update_layout(
                title="Folder topology (size-weighted)",
                margin=dict(t=40, l=10, r=10, b=10),
            )
            chart_html = render_plotly(fig)

        artifact.paths.append(write_html(
            out_dir / "topology.html",
            "topology.html.j2",
            {
                "title": "Topology",
                "run": run_summary(conn, run_id),
                "rows": rows,
                "max_depth": 4,
                "chart": chart_html,
            },
        ))
    return artifact
