"""Folder-path tag extraction.

Goal: turn `Z:\\maschinen\\12345\\anleitungen\\gasmesser\\xyz.pdf` into
`['maschinen', '12345', 'anleitungen', 'gasmesser']`. Pure speed, no
ontology, no scoring, no config. Downstream RAG can filter by tags
directly.

The function must survive messy real-world paths:
- Windows drive letters (`Z:\\…`) and UNC roots (`\\\\server\\share\\…`)
- Long-path prefixes (`\\\\?\\…`, `\\\\?\\UNC\\…`)
- Mixed separators within one path
- Trailing separators, double separators, leading whitespace
- Hidden / system folders (`.git`, `_archive`, `$RECYCLE.BIN`)
- Pathologically deep trees and pathologically long folder names
- Unicode (`/srv/Projets/Élise/notes.md`)
- Empty / filename-only / None
"""
from __future__ import annotations

from typing import Iterable

# Folder names that are pure organisational chrome — never useful as
# RAG tags. Matched case-insensitively after stripping.
BLOCKLIST: frozenset[str] = frozenset({
    "shared",
    "backup",
    "backups",
    "temp",
    "tmp",
    "old",
    "archive",
    "archives",
    "misc",
    "final",
    "new",
    "copy",
    "copies",
    "documents",
    "files",
    "stuff",
    "draft",
    "drafts",
    "untitled folder",
    # Windows long-path prefix component once the leading `\\?\` is
    # stripped — protects against `\\?\UNC\…` paths leaking 'unc' as
    # a tag.
    "unc",
})

# Soft caps. Pathologically deep trees (50+ levels) bloat storage and
# pathologically long folder names ("RFP_Response_Final_v2 (Reviewed
# by Legal 2024-03-14)") are useless as tags. Tunable but the defaults
# are deliberately generous.
MAX_TAGS: int = 16
MAX_TAG_LEN: int = 64

_LONG_PATH_PREFIX = "\\\\?\\"
_LONG_PATH_UNC_PREFIX = "\\\\?\\UNC\\"


def _strip_long_path_prefix(path: str) -> str:
    """Remove the Windows `\\\\?\\` long-path prefix if present.

    `\\\\?\\UNC\\server\\share\\…` becomes `\\\\server\\share\\…`, and
    `\\\\?\\Z:\\…` becomes `Z:\\…`. Anything else passes through.
    """
    if path.startswith(_LONG_PATH_UNC_PREFIX):
        return "\\\\" + path[len(_LONG_PATH_UNC_PREFIX):]
    if path.startswith(_LONG_PATH_PREFIX):
        return path[len(_LONG_PATH_PREFIX):]
    return path


def _is_drive_letter(folder: str) -> bool:
    """True for `C:`, `Z:`, `c:`, etc. — Windows drive roots."""
    return len(folder) == 2 and folder[1] == ":" and folder[0].isalpha()


def _iter_folders(path: str) -> Iterable[str]:
    """Yield each folder segment of `path`, excluding the filename.

    Tolerates mixed separators, trailing separators, double separators,
    and the long-path prefix.
    """
    if not path:
        return
    p = _strip_long_path_prefix(path)
    # Normalise separators in one pass; cheap. We deliberately split on
    # '/' AFTER replacement so '\\\\server\\share' produces leading
    # empty strings that the caller drops with the `if not folder`
    # check, rather than treating the UNC server as a tag prefix.
    parts = p.replace("\\", "/").split("/")
    # Drop the filename — anything after the last separator.
    for folder in parts[:-1]:
        yield folder


def extract_tags(path: str | None) -> list[str]:
    """Return ordered, deduped folder tags for `path`.

    See module docstring for the messy-path guarantees this enforces.
    Returns an empty list for falsy input. Always returns a fresh list
    so callers can mutate freely.
    """
    if not path:
        return []

    tags: list[str] = []
    seen: set[str] = set()

    for raw in _iter_folders(path):
        folder = raw.strip()
        if not folder:
            # Empty segments come from leading `//`, double slashes, or
            # the empty prefix of a UNC path. Always drop them.
            continue
        if _is_drive_letter(folder):
            # `Z:` is location, not content.
            continue
        # Hidden / private / system conventions — same on Windows and
        # POSIX. Skip BEFORE lowercasing because `$RECYCLE.BIN` is a
        # case-insensitive convention but the prefix check is exact.
        if folder[0] in ("_", ".", "$"):
            continue

        tag = folder.lower()
        if len(tag) < 2 or len(tag) > MAX_TAG_LEN:
            # Single-char folders ("A/B/C/file") are rarely meaningful;
            # very long folder names ("RFP_v2 (Reviewed by Legal …)")
            # bloat storage without adding signal.
            continue
        if tag in BLOCKLIST:
            continue
        if tag in seen:
            # `Foo/foo/bar.pdf` shouldn't emit `foo` twice.
            continue

        seen.add(tag)
        tags.append(tag)
        if len(tags) >= MAX_TAGS:
            # Pathologically deep paths get clipped — this is a tag
            # set, not a path index.
            break

    return tags
