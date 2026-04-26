"""Optional persistent defaults loaded from share-analyzer.toml."""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any


CONFIG_FILENAME = "share-analyzer.toml"

# Glob patterns that match the standard nuisance files real Windows /
# SMB shares accumulate. Matched against entry name and full path via
# fnmatch in the walker. Applied unless `--no-default-excludes` is set
# or `[scan].default_excludes = false` in share-analyzer.toml.
#
# Curated to be safe: every entry is either a system-generated artifact
# (Office lock files, recycle bin, OS metadata) or a temp-file convention
# that's never a RAG candidate. Listed alphabetically for review.
DEFAULT_EXCLUDES: tuple[str, ...] = (
    "$RECYCLE.BIN",
    ".DS_Store",
    ".~lock.*#",
    "Thumbs.db",
    "Thumbs.db:encryptable",
    "__MACOSX",
    "desktop.ini",
    "ehthumbs.db",
    "hiberfil.sys",
    "pagefile.sys",
    "swapfile.sys",
    "~$*",
    "~*.tmp",
    "*.tmp",
)


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
