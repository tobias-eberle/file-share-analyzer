"""Top folders by total size at depths 1–4."""
from __future__ import annotations

from pathlib import Path

import plotly.graph_objects as go

from share_analyzer.index.queries import run_summary, size_hotspots
from share_analyzer.reports.base import (
    ReportArtifact, register, render_plotly, write_csv, write_html,
)


@register("size_hotspots", "Largest folders at depths 1–4", ("html", "csv"))
def report_size_hotspots(*, conn, run_id, out_dir: Path, formats, limit: int = 50):
    levels: list[tuple[int, list[dict]]] = []
    for level in range(1, 5):
        levels.append((level, size_hotspots(conn, run_id, level=level, limit=limit)))

    artifact = ReportArtifact(name="size_hotspots", paths=[])

    if "csv" in formats:
        flat = []
        for level, rows in levels:
            for r in rows:
                flat.append({"depth": level, **r})
        artifact.paths.append(write_csv(out_dir / "size_hotspots.csv", flat))

    if "html" in formats:
        charts: dict[int, str] = {}
        for level, rows in levels:
            if not rows:
                charts[level] = ""
                continue
            fig = go.Figure(go.Bar(
                x=[r["total_size"] or 0 for r in rows][::-1],
                y=[r["path"] for r in rows][::-1],
                orientation="h",
            ))
            fig.update_layout(
                title=f"Top {limit} folders at depth {level}",
                xaxis_title="Total size (bytes)",
                yaxis_title="Folder",
                margin=dict(t=40, l=10, r=10, b=10),
                height=600,
            )
            charts[level] = render_plotly(fig)

        artifact.paths.append(write_html(
            out_dir / "size_hotspots.html",
            "size_hotspots.html.j2",
            {
                "title": "Size hotspots",
                "run": run_summary(conn, run_id),
                "levels": levels,
                "charts": charts,
                "limit": limit,
            },
        ))
    return artifact
