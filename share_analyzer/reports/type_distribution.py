"""File counts and total size per MIME category and per top-20 extension."""
from __future__ import annotations

from pathlib import Path

import plotly.graph_objects as go

from share_analyzer.index.queries import (
    category_distribution, extension_distribution, run_summary,
)
from share_analyzer.reports.base import (
    ReportArtifact, register, render_plotly, write_csv, write_html,
)


@register("type_distribution", "MIME categories and top extensions", ("html", "csv"))
def report_type_distribution(*, conn, run_id, out_dir: Path, formats):
    categories = category_distribution(conn, run_id)
    extensions = extension_distribution(conn, run_id, limit=20)

    artifact = ReportArtifact(name="type_distribution", paths=[])

    if "csv" in formats:
        artifact.paths.append(
            write_csv(out_dir / "type_distribution_categories.csv", categories)
        )
        artifact.paths.append(
            write_csv(out_dir / "type_distribution_extensions.csv", extensions)
        )

    if "html" in formats:
        cat_chart = ""
        if categories:
            fig = go.Figure(go.Pie(
                labels=[r["category"] for r in categories],
                values=[r["total_size"] or 0 for r in categories],
            ))
            fig.update_layout(
                title="Bytes by MIME category",
                margin=dict(t=40, l=10, r=10, b=10),
            )
            cat_chart = render_plotly(fig)

        ext_chart = ""
        if extensions:
            fig = go.Figure(go.Bar(
                x=[r["extension"] for r in extensions],
                y=[r["file_count"] for r in extensions],
            ))
            fig.update_layout(
                title="Top extensions by file count",
                xaxis_title="Extension",
                yaxis_title="Files",
                margin=dict(t=40, l=10, r=10, b=10),
            )
            ext_chart = render_plotly(fig)

        artifact.paths.append(write_html(
            out_dir / "type_distribution.html",
            "type_distribution.html.j2",
            {
                "title": "Type distribution",
                "run": run_summary(conn, run_id),
                "categories": categories,
                "extensions": extensions,
                "category_chart": cat_chart,
                "extension_chart": ext_chart,
            },
        ))
    return artifact
