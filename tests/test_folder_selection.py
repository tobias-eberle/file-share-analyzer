"""Folder-tree subfolder selection: model + walker integration."""
from __future__ import annotations

from pathlib import Path

from share_analyzer.crawl.orchestrator import CrawlOptions, run_crawl
from share_analyzer.crawl.walker import FileEntry, LocalScandirWalker
from share_analyzer.index.schema import connect
from share_analyzer.ui.folder_selection import (
    EXCLUDED, INCLUDED, FolderSelection, list_subfolders,
)


# ----------------------------------------------------------------------
# FolderSelection — pure logic
# ----------------------------------------------------------------------


def test_default_state_is_included():
    s = FolderSelection("/srv/share")
    assert s.state_of("/srv/share") == INCLUDED
    assert s.state_of("/srv/share/anything") == INCLUDED


def test_toggle_flips_between_states():
    s = FolderSelection("/srv/share")
    assert s.toggle("/srv/share/legacy") == EXCLUDED
    assert s.state_of("/srv/share/legacy") == EXCLUDED
    assert s.toggle("/srv/share/legacy") == INCLUDED


def test_excluded_paths_returns_minimal_set():
    """A child of an already-excluded parent shouldn't appear in the
    set passed to the walker — the walker doesn't descend into a
    pruned subtree, so listing descendants is wasted bytes."""
    s = FolderSelection("/srv/share")
    s.set_state("/srv/share/legacy", EXCLUDED)
    s.set_state("/srv/share/legacy/old", EXCLUDED)
    s.set_state("/srv/share/legacy/old/2018", EXCLUDED)
    s.set_state("/srv/share/videos", EXCLUDED)

    excluded = s.excluded_paths()
    # Only the topmost ancestors of each excluded subtree.
    assert set(excluded) == {"/srv/share/legacy", "/srv/share/videos"}


def test_excluded_paths_no_false_positive_on_path_prefix():
    """`/foo` and `/foobar` share a string prefix but aren't related."""
    s = FolderSelection("/foo")
    s.set_state("/foo/bar", EXCLUDED)
    s.set_state("/foobar", EXCLUDED)  # not a descendant of /foo/bar

    assert set(s.excluded_paths()) == {"/foo/bar", "/foobar"}


def test_is_effectively_excluded_walks_to_root():
    s = FolderSelection("/srv/share")
    s.set_state("/srv/share/legacy", EXCLUDED)
    assert s.is_effectively_excluded("/srv/share/legacy")
    assert s.is_effectively_excluded("/srv/share/legacy/2018")
    assert s.is_effectively_excluded("/srv/share/legacy/2018/q1")
    assert not s.is_effectively_excluded("/srv/share/projects")


def test_has_excluded_descendants_drives_mixed_glyph():
    s = FolderSelection("/srv/share")
    s.set_state("/srv/share/projects/legacy", EXCLUDED)
    assert s.has_excluded_descendants("/srv/share")
    assert s.has_excluded_descendants("/srv/share/projects")
    assert not s.has_excluded_descendants("/srv/share/projects/legacy")  # itself
    assert not s.has_excluded_descendants("/srv/share/other")


def test_remember_does_not_overwrite_existing_state():
    s = FolderSelection("/srv/share")
    s.set_state("/srv/share/legacy", EXCLUDED)
    s.remember("/srv/share/legacy")
    assert s.state_of("/srv/share/legacy") == EXCLUDED


def test_set_state_rejects_unknown_state():
    s = FolderSelection("/srv/share")
    try:
        s.set_state("/srv/share/x", "garbage")
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown state")


def test_list_subfolders_returns_sorted_dirs(tmp_path: Path):
    (tmp_path / "Charlie").mkdir()
    (tmp_path / "alpha").mkdir()
    (tmp_path / "Bravo").mkdir()
    (tmp_path / "a-file.txt").write_text("not a dir")
    result = list_subfolders(tmp_path)
    # Sort is case-insensitive by name.
    names = [Path(p).name for p in result]
    assert names == ["alpha", "Bravo", "Charlie"]


def test_list_subfolders_empty_for_unreadable(tmp_path: Path):
    """Permission denied / missing directory must return [], not raise —
    the dialog populates lazily and a single bad subfolder shouldn't
    kill the whole tree."""
    assert list_subfolders(tmp_path / "does-not-exist") == []


# ----------------------------------------------------------------------
# Walker prunes excluded_paths
# ----------------------------------------------------------------------


def _build_branchy(tmp_path: Path) -> Path:
    root = tmp_path / "share"
    for sub in ("alpha", "beta", "legacy", "videos"):
        d = root / sub
        d.mkdir(parents=True)
        for i in range(2):
            (d / f"f{i}.txt").write_text(f"{sub}-{i}")
    nested = root / "legacy" / "old"
    nested.mkdir()
    (nested / "deep.txt").write_text("ancient")
    return root


def test_walker_skips_excluded_subtree(tmp_path: Path):
    root = _build_branchy(tmp_path)
    excluded = (str(root / "legacy"), str(root / "videos"))
    w = LocalScandirWalker(root, excluded_paths=excluded, dir_workers=1)
    files = sorted(i.path for i in w.walk() if isinstance(i, FileEntry))
    # alpha + beta survive (2 files each), legacy + videos pruned.
    assert len(files) == 4
    for p in files:
        assert "/legacy/" not in p and "\\legacy\\" not in p
        assert "/videos/" not in p and "\\videos\\" not in p


def test_walker_excluded_paths_works_in_parallel_mode(tmp_path: Path):
    """The same pruning must hold under dir_workers > 1."""
    root = _build_branchy(tmp_path)
    excluded = (str(root / "legacy"),)
    w = LocalScandirWalker(root, excluded_paths=excluded, dir_workers=4)
    files = sorted(i.path for i in w.walk() if isinstance(i, FileEntry))
    assert all("legacy" not in p for p in files)
    # alpha + beta + videos = 6 files.
    assert len(files) == 6


def test_walker_excluded_paths_normalises_input(tmp_path: Path):
    """Caller passing a Path-with-trailing-slash or a Path object should
    still work. The walker normalises on construction."""
    root = _build_branchy(tmp_path)
    excluded = [
        str(root / "legacy") + "/",   # trailing slash
        Path(root / "videos"),         # Path object via str(Path(...))
    ]
    w = LocalScandirWalker(root, excluded_paths=excluded)
    files = sorted(i.path for i in w.walk() if isinstance(i, FileEntry))
    assert all("legacy" not in p and "videos" not in p for p in files)
    assert len(files) == 4


def test_walker_does_not_descend_into_excluded_child(tmp_path: Path):
    """Even if a *grandchild* would otherwise emit a file, excluding
    its parent stops the walker from descending."""
    root = _build_branchy(tmp_path)
    w = LocalScandirWalker(
        root, excluded_paths=(str(root / "legacy"),), dir_workers=1,
    )
    files = [i.path for i in w.walk() if isinstance(i, FileEntry)]
    # `legacy/old/deep.txt` would otherwise be there; assert it isn't.
    assert not any(p.endswith("deep.txt") for p in files)


# ----------------------------------------------------------------------
# Orchestrator end-to-end: excluded subtree never reaches the index
# ----------------------------------------------------------------------


def test_orchestrator_round_trip_with_excluded_paths(tmp_path: Path):
    root = _build_branchy(tmp_path)
    db = tmp_path / "i.sqlite"
    result = run_crawl(
        root, db,
        CrawlOptions(workers=2, excluded_paths=(str(root / "legacy"),)),
    )
    assert result.status == "completed"

    with connect(db) as conn:
        paths = {r[0] for r in conn.execute(
            "SELECT path FROM files WHERE run_id = ?", (result.run_id,)
        )}
    assert paths
    for p in paths:
        assert "legacy" not in p, (
            f"excluded subtree leaked into the index: {p}"
        )


def test_excluded_paths_via_resolve_then_walker(tmp_path: Path):
    """Full chain: FolderSelection → excluded_paths() → walker."""
    root = _build_branchy(tmp_path)

    s = FolderSelection(root)
    s.set_state(str(root / "legacy"), EXCLUDED)
    s.set_state(str(root / "videos"), EXCLUDED)
    excluded = s.excluded_paths()

    db = tmp_path / "i.sqlite"
    result = run_crawl(root, db, CrawlOptions(workers=2,
                                                excluded_paths=excluded))
    with connect(db) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM files WHERE run_id = ?", (result.run_id,)
        ).fetchone()[0]
    # alpha (2) + beta (2) = 4
    assert n == 4
