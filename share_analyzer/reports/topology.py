"""Folder tree CSV — visualised in the dashboard, exported here for
downstream tooling that wants the raw rows."""
from __future__ import annotations

from pathlib import Path

from share_analyzer.index.queries import topology
from share_analyzer.reports.base import ReportArtifact, register, write_csv


@register("topology", "Folder tree CSV (visualised in dashboard.html)", ("csv",))
def report_topology(*, conn, run_id, out_dir: Path, formats):
    artifact = ReportArtifact(name="topology", paths=[])
    if "csv" in formats:
        rows = topology(conn, run_id, max_depth=4)
        artifact.paths.append(write_csv(out_dir / "topology.csv", rows))
    return artifact
