"""RAG candidate export — the hand-off artifact for the ingestion pipeline."""
from __future__ import annotations

from pathlib import Path

from share_analyzer.index.queries import rag_candidates
from share_analyzer.reports.base import (
    ReportArtifact, register, write_jsonl,
)

DEFAULT_MIN_BYTES = 1024
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_AGE_DAYS = 365 * 5


def _suggest_category(mime_type: str | None, ext: str | None) -> str:
    if not mime_type:
        return "unknown"
    mt = mime_type.lower()
    if "pdf" in mt:
        return "pdf"
    if "wordprocessing" in mt or mt == "application/msword":
        return "word"
    if "spreadsheet" in mt or "excel" in mt:
        return "spreadsheet"
    if "presentation" in mt or "powerpoint" in mt:
        return "presentation"
    if mt.startswith("text/markdown") or ext == ".md":
        return "markdown"
    if mt.startswith("text/html") or mt == "application/xhtml+xml":
        return "html"
    if mt.startswith("text/"):
        return "plaintext"
    if mt == "message/rfc822" or ext in (".eml", ".msg"):
        return "email"
    return "other-text"


@register("rag_candidates", "JSONL of files matching ingestion filters", ("jsonl",))
def report_rag_candidates(
    *, conn, run_id, out_dir: Path, formats,
    min_size: int = DEFAULT_MIN_BYTES,
    max_size: int = DEFAULT_MAX_BYTES,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
):
    out_path = out_dir / "rag_candidates.jsonl"

    def candidate_rows():
        for row in rag_candidates(
            conn, run_id,
            min_size=min_size, max_size=max_size,
            max_age_days=max_age_days,
        ):
            yield {
                "path": row["path"],
                "size": row["size"],
                "mtime": row["mtime"],
                "sha256": row["sha256"],
                "mime_type": row["mime_type"],
                "mime_category": row["mime_category"],
                "extension": row["extension"],
                "suggested_category": _suggest_category(
                    row["mime_type"], row["extension"]
                ),
            }

    artifact = ReportArtifact(name="rag_candidates", paths=[])
    if "jsonl" in formats or "all" in formats:
        artifact.paths.append(write_jsonl(out_path, candidate_rows()))
    return artifact
