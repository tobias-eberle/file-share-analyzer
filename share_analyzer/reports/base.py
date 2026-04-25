"""Report runner — wires queries → templates → output files."""
from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(("html",)),
    trim_blocks=True,
    lstrip_blocks=True,
)


def _human_size(n: int | None) -> str:
    if n is None:
        return ""
    n = int(n)
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    f = float(n)
    for u in units:
        if f < 1024.0 or u == units[-1]:
            return f"{f:,.1f} {u}" if u != "B" else f"{int(f):,} {u}"
        f /= 1024.0
    return f"{f:,.1f} {units[-1]}"


_env.filters["human_size"] = _human_size


@dataclass
class ReportArtifact:
    name: str
    paths: list[Path]


@dataclass
class ReportSpec:
    name: str
    description: str
    formats: tuple[str, ...]
    runner: Callable[..., ReportArtifact]


REPORTS: dict[str, ReportSpec] = {}


def register(name: str, description: str, formats: tuple[str, ...]):
    def deco(fn):
        REPORTS[name] = ReportSpec(name, description, formats, fn)
        return fn
    return deco


def write_csv(out_path: Path, rows: Iterable[dict[str, Any]],
              headers: Optional[list[str]] = None) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = list(rows)
    if headers is None:
        headers = list(rows[0].keys()) if rows else []
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return out_path


def write_jsonl(out_path: Path, rows: Iterable[dict[str, Any]]) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return out_path


def write_html(out_path: Path, template: str, context: dict[str, Any]) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = _env.get_template(template).render(**context)
    out_path.write_text(rendered, encoding="utf-8")
    return out_path


def render_plotly(fig) -> str:
    """Inline a Plotly figure as a self-contained HTML snippet (no CDN)."""
    return fig.to_html(
        include_plotlyjs="inline",
        full_html=False,
        config={"displaylogo": False},
    )


def run_report(name: str, conn: sqlite3.Connection, run_id: int,
               out_dir: Path, fmt: str = "all") -> ReportArtifact:
    if name not in REPORTS:
        raise KeyError(f"unknown report: {name}")
    spec = REPORTS[name]
    out_dir.mkdir(parents=True, exist_ok=True)
    formats = spec.formats if fmt == "all" else (fmt,)
    return spec.runner(conn=conn, run_id=run_id, out_dir=out_dir, formats=formats)


def run_all(conn: sqlite3.Connection, run_id: int,
            out_dir: Path) -> list[ReportArtifact]:
    return [run_report(name, conn, run_id, out_dir, "all") for name in REPORTS]


# Importing the report modules registers them via the @register decorator.
from share_analyzer.reports import (  # noqa: E402  — side-effecting imports
    topology, size_hotspots, staleness, duplication,
    type_distribution, rag_candidates,
)
