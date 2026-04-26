"""Pre-flight summary: walks a path and prints stats without writing
to the index. Used by `share-analyzer scan --dry-run`.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from share_analyzer.crawl.retry import DEFAULT_BACKOFF
from share_analyzer.crawl.walker import (
    FileEntry, LocalScandirWalker, WalkError, Walker,
)


@dataclass
class DryRunSummary:
    file_count: int = 0
    folder_count: int = 0
    error_count: int = 0
    total_size: int = 0
    extensions: Counter[str] = field(default_factory=Counter)
    sample_paths: list[str] = field(default_factory=list)
    sample_errors: list[tuple[str, str]] = field(default_factory=list)


_SAMPLE_LIMIT = 5


def dry_run(
    root: str | Path,
    *,
    exclude_globs: Sequence[str] = (),
    follow_symlinks: bool = False,
    retry_backoff: Sequence[float] = DEFAULT_BACKOFF,
    dir_workers: int = 1,
    walker: Walker | None = None,
) -> DryRunSummary:
    """Walk `root` once, count and sample, return a summary.

    No SQLite, no fingerprinting. Designed to be cheap enough to run on
    a real share before committing to a full scan.
    """
    walker = walker or LocalScandirWalker(
        root,
        exclude_globs=exclude_globs,
        follow_symlinks=follow_symlinks,
        retry_backoff=retry_backoff,
        dir_workers=dir_workers,
    )
    summary = DryRunSummary()
    seen_parents: set[str] = set()

    for item in walker.walk():
        if isinstance(item, WalkError):
            summary.error_count += 1
            if len(summary.sample_errors) < _SAMPLE_LIMIT:
                summary.sample_errors.append((item.path, item.reason))
            continue
        if not isinstance(item, FileEntry):  # pragma: no cover — defensive
            continue
        summary.file_count += 1
        summary.total_size += item.size
        summary.extensions[item.extension or "(none)"] += 1
        if item.parent_path not in seen_parents:
            seen_parents.add(item.parent_path)
        if len(summary.sample_paths) < _SAMPLE_LIMIT:
            summary.sample_paths.append(item.path)
    summary.folder_count = len(seen_parents)
    return summary


def _human_size(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    f = float(n)
    for u in units:
        if f < 1024.0 or u == units[-1]:
            return f"{f:,.1f} {u}" if u != "B" else f"{int(f):,} {u}"
        f /= 1024.0
    return f"{f:,.1f} {units[-1]}"


def format_summary(summary: DryRunSummary, *, top_extensions: int = 10) -> str:
    """Render the summary as the text the CLI prints."""
    lines = [
        "[dry-run]",
        f"  files       : {summary.file_count:,}",
        f"  folders     : {summary.folder_count:,}",
        f"  total size  : {_human_size(summary.total_size)}",
        f"  errors      : {summary.error_count:,}",
    ]
    if summary.extensions:
        lines.append(f"  top {top_extensions} extensions:")
        for ext, count in summary.extensions.most_common(top_extensions):
            lines.append(f"    {ext or '(none)':<12} {count:>10,}")
    if summary.sample_paths:
        lines.append("  sample paths:")
        for p in summary.sample_paths:
            lines.append(f"    - {p}")
    if summary.sample_errors:
        lines.append("  sample errors:")
        for path, reason in summary.sample_errors:
            lines.append(f"    - {path}: {reason}")
    return "\n".join(lines)
