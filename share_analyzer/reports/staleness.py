"""Staleness bucket CSV (visualised in dashboard.html)."""
from __future__ import annotations

from pathlib import Path

from share_analyzer.index.queries import staleness_buckets
from share_analyzer.reports.base import ReportArtifact, register, write_csv


@register("staleness",
           "Files by mtime bucket (visualised in dashboard.html)",
           ("csv",))
def report_staleness(*, conn, run_id, out_dir: Path, formats):
    artifact = ReportArtifact(name="staleness", paths=[])
    if "csv" in formats:
        artifact.paths.append(
            write_csv(out_dir / "staleness.csv", staleness_buckets(conn, run_id))
        )
    return artifact
