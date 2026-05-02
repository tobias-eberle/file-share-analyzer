"""Folder-tree selection model — the Tk-free part of the dialog.

The Tk view in `app.py` is a thin wrapper around this class: it
lazy-loads children via `os.scandir`, mirrors the per-path state
into a Treeview, and toggles via a button. All the rules — what
"excluded" means, parent-child propagation, the derived set the
walker actually consumes — live here so we can test them headlessly.

State model:
- Default: every folder is `included`. The user un-checks subtrees
  they don't want indexed.
- Toggling a folder flips it between `included` and `excluded`.
- A folder under an `excluded` ancestor is *effectively* excluded
  regardless of its own state. We don't try to support "exclude the
  parent but include this one descendant" — the walker would have
  to descend into a pruned subtree to honour it, and the UX is
  confusing.

`excluded_paths()` returns the minimal set the walker needs: only
the topmost excluded path of each excluded subtree, since the walker
will not descend into a path it has already been told to skip.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Iterator, Optional


INCLUDED = "included"
EXCLUDED = "excluded"


class FolderSelection:
    def __init__(self, root: str | Path) -> None:
        # Normalise via Path so mixed slashes / trailing seps don't
        # produce different keys for the same folder.
        self.root: str = str(Path(root))
        self._state: dict[str, str] = {self.root: INCLUDED}

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def set_state(self, path: str | Path, state: str) -> None:
        if state not in (INCLUDED, EXCLUDED):
            raise ValueError(f"unknown state: {state!r}")
        self._state[str(Path(path))] = state

    def toggle(self, path: str | Path) -> str:
        """Flip the state of `path` between included and excluded.
        Returns the new state."""
        key = str(Path(path))
        new = EXCLUDED if self._state.get(key, INCLUDED) == INCLUDED else INCLUDED
        self._state[key] = new
        return new

    def remember(self, path: str | Path) -> None:
        """Note that the dialog has shown `path`, defaulting to
        included. Lets us distinguish "user has seen this folder" from
        "we don't know about it yet" — useful when the user expands
        the tree to load more children."""
        key = str(Path(path))
        self._state.setdefault(key, INCLUDED)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def state_of(self, path: str | Path) -> str:
        return self._state.get(str(Path(path)), INCLUDED)

    def is_effectively_excluded(self, path: str | Path) -> bool:
        """True if `path` itself or any of its ancestors up to the
        root is excluded. The walker uses `excluded_paths()` directly,
        but the view needs this to grey out descendants of an
        excluded folder."""
        p = Path(path)
        while True:
            sp = str(p)
            if self._state.get(sp) == EXCLUDED:
                return True
            if sp == self.root or p.parent == p:
                return False
            p = p.parent

    def has_excluded_descendants(self, path: str | Path) -> bool:
        """Returns True if any *known* descendant of `path` is excluded.
        Drives the 'mixed' indicator in the view."""
        prefix = str(Path(path))
        # Compare with a separator suffix so /foo doesn't match /foobar.
        prefix_sep = prefix + ("\\" if "\\" in prefix else "/")
        for sp, state in self._state.items():
            if state != EXCLUDED:
                continue
            if sp == prefix:
                continue
            if sp.startswith(prefix_sep):
                return True
        return False

    # ------------------------------------------------------------------
    # Walker handoff
    # ------------------------------------------------------------------

    def excluded_paths(self) -> tuple[str, ...]:
        """Minimal set of paths the walker must skip.

        Drops any excluded path whose parent is also excluded — the
        walker will not descend into a pruned subtree, so listing
        descendants is wasted bytes and risks rounding errors on
        path normalisation.
        """
        excluded = sorted(p for p, s in self._state.items() if s == EXCLUDED)
        result: list[str] = []
        for p in excluded:
            if any(self._is_descendant(p, kept) for kept in result):
                continue
            result.append(p)
        return tuple(result)

    @staticmethod
    def _is_descendant(child: str, parent: str) -> bool:
        if child == parent:
            return False
        if "\\" in parent:
            return child.startswith(parent + "\\")
        return child.startswith(parent + "/")

    # ------------------------------------------------------------------
    # Iteration helpers — debug / test-only
    # ------------------------------------------------------------------

    def known_paths(self) -> Iterator[str]:
        return iter(self._state)

    def __len__(self) -> int:
        return len(self._state)


def list_subfolders(folder: str | Path) -> list[str]:
    """Subdirectories of `folder`, sorted by name.

    Used by the Tk dialog to lazy-populate the tree on expand. Lives
    here (not in the view) so test code can drive it without spinning
    up Tk. Falls back to an empty list on permission / OS errors so
    the dialog doesn't die on a single unreadable folder.
    """
    import os
    try:
        with os.scandir(str(folder)) as it:
            kids = []
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        kids.append(entry.path)
                except OSError:
                    continue
        return sorted(kids, key=lambda p: Path(p).name.lower())
    except (PermissionError, FileNotFoundError, NotADirectoryError, OSError):
        return []
