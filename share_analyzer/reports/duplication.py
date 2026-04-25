"""Top duplicate clusters by wasted space."""
from __future__ import annotations

import json
from pathlib import Path

from share_analyzer.index.queries import run_summary, top_duplicates
from share_analyzer.reports.base import (
    ReportArtifact, register, write_csv, write_html,
)


@register("duplication", "Duplicate file clusters", ("html", "csv"))
def report_duplication(*, conn, run_id, out_dir: Path, formats, limit: int = 100):
    rows = top_duplicates(conn, run_id, limit=limit)
    total_wasted = sum(r["wasted_bytes"] for r in rows)
    artifact = ReportArtifact(name="duplication", paths=[])

    if "csv" in formats:
        flat = [
            {**{k: v for k, v in r.items() if k != "sample_paths"},
             "sample_paths": json.dumps(r["sample_paths"])}
            for r in rows
        ]
        artifact.paths.append(write_csv(out_dir / "duplication.csv", flat))

    if "html" in formats:
        artifact.paths.append(write_html(
            out_dir / "duplication.html",
            "duplication.html.j2",
            {
                "title": "Duplication",
                "run": run_summary(conn, run_id),
                "rows": rows,
                "total_wasted": total_wasted,
            },
        ))
    return artifact
