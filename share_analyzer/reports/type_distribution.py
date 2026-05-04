"""Type-distribution CSVs (visualised in dashboard.html)."""
from __future__ import annotations

from pathlib import Path

from share_analyzer.index.queries import (
    category_distribution, extension_distribution,
)
from share_analyzer.reports.base import ReportArtifact, register, write_csv


@register("type_distribution",
           "MIME categories + top extensions (visualised in dashboard.html)",
           ("csv",))
def report_type_distribution(*, conn, run_id, out_dir: Path, formats):
    artifact = ReportArtifact(name="type_distribution", paths=[])
    if "csv" in formats:
        artifact.paths.append(write_csv(
            out_dir / "type_distribution_categories.csv",
            category_distribution(conn, run_id),
        ))
        artifact.paths.append(write_csv(
            out_dir / "type_distribution_extensions.csv",
            extension_distribution(conn, run_id, limit=20),
        ))
    return artifact
