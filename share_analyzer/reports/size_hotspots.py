"""Size hotspots CSV — top folders at depths 1–4. Visualised in the
dashboard; this CSV is for downstream tooling."""
from __future__ import annotations

from pathlib import Path

from share_analyzer.index.queries import size_hotspots
from share_analyzer.reports.base import ReportArtifact, register, write_csv


@register("size_hotspots",
           "Top folders at depths 1–4 (visualised in dashboard.html)",
           ("csv",))
def report_size_hotspots(*, conn, run_id, out_dir: Path, formats,
                          limit: int = 50):
    artifact = ReportArtifact(name="size_hotspots", paths=[])
    if "csv" in formats:
        flat = []
        for level in range(1, 5):
            for r in size_hotspots(conn, run_id, level=level, limit=limit):
                flat.append({"depth": level, **r})
        artifact.paths.append(write_csv(out_dir / "size_hotspots.csv", flat))
    return artifact
