"""Optional persistent defaults loaded from share-analyzer.toml."""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any


CONFIG_FILENAME = "share-analyzer.toml"


def find_config(start: Path) -> Path | None:
    cur = start.resolve()
    for parent in (cur, *cur.parents):
        candidate = parent / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


def load_config(path: Path | None = None, *, start: Path | None = None) -> dict[str, Any]:
    p = path or find_config(start or Path.cwd())
    if p is None:
        return {}
    with p.open("rb") as f:
        return tomllib.load(f)
