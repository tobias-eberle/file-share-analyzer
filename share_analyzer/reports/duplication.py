"""Duplicate-cluster CSV (visualised in dashboard.html)."""
from __future__ import annotations

import json
from pathlib import Path

from share_analyzer.index.queries import top_duplicates
from share_analyzer.reports.base import ReportArtifact, register, write_csv


@register("duplication",
           "Duplicate file clusters (visualised in dashboard.html)",
           ("csv",))
def report_duplication(*, conn, run_id, out_dir: Path, formats,
                        limit: int = 100):
    artifact = ReportArtifact(name="duplication", paths=[])
    if "csv" in formats:
        rows = top_duplicates(conn, run_id, limit=limit)
        # `sample_paths` is a list per row; flatten to a JSON string so
        # the CSV stays a single value per cell.
        flat = [
            {**{k: v for k, v in r.items() if k != "sample_paths"},
             "sample_paths": json.dumps(r["sample_paths"])}
            for r in rows
        ]
        artifact.paths.append(write_csv(out_dir / "duplication.csv", flat))
    return artifact
