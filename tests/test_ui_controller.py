"""UI Controller — the Tk-free part. Background threads + event queue."""
from __future__ import annotations

import queue
import time
from pathlib import Path

import pytest

from share_analyzer.crawl.orchestrator import CrawlOptions, run_crawl
from share_analyzer.ui.controller import (
    Controller, Event, RUN_COLUMNS, format_run_row,
)


def _build_share(tmp_path: Path) -> Path:
    root = tmp_path / "share"
    sub = root / "docs"
    sub.mkdir(parents=True)
    (sub / "a.txt").write_text("alpha")
    (sub / "b.txt").write_text("beta")
    return root


def _drain(c: Controller, *, until_kind: str, timeout: float = 10.0,
            allow_errors: bool = False) -> Event:
    """Block until the controller posts an event of `until_kind`,
    discarding intermediate progress events. By default any `error`
    event fails the test loudly; pass `allow_errors=True` if you're
    deliberately exercising a rejection."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            ev = c.events.get(timeout=0.2)
        except queue.Empty:
            continue
        if ev.kind == until_kind:
            return ev
        if ev.kind == "error" and not allow_errors:
            raise AssertionError(
                f"unexpected error event: {ev.payload.get('message')}"
            )
    raise AssertionError(f"never saw event {until_kind!r}")


# ----------------------------------------------------------------------
# Read-only helpers
# ----------------------------------------------------------------------


def test_list_runs_returns_empty_for_missing_db(tmp_path: Path):
    c = Controller()
    assert c.list_runs(tmp_path / "nope.sqlite") == []


def test_list_runs_returns_empty_for_corrupt_db(tmp_path: Path):
    c = Controller()
    bad = tmp_path / "bad.sqlite"
    bad.write_bytes(b"this is not a sqlite database")
    assert c.list_runs(bad) == []


def test_list_runs_after_scan(tmp_path: Path):
    root = _build_share(tmp_path)
    db = tmp_path / "i.sqlite"
    run_crawl(root, db, CrawlOptions(workers=2))

    c = Controller()
    runs = c.list_runs(db)
    assert len(runs) == 1
    r = runs[0]
    assert r["status"] == "completed"
    assert r["file_count"] == 2
    assert r["root_path"] == str(root)


def test_run_details_includes_state_counts_for_rescan(tmp_path: Path):
    root = _build_share(tmp_path)
    db = tmp_path / "i.sqlite"
    base = run_crawl(root, db, CrawlOptions(workers=2))

    (root / "docs" / "c.txt").write_text("gamma")
    rescan = run_crawl(
        root, db,
        CrawlOptions(workers=2, previous_run_id=base.run_id),
    )

    c = Controller()
    d = c.run_details(db, rescan.run_id)
    assert d["status"] == "completed"
    assert d["state_counts"]["added"] == 1
    assert d["state_counts"]["unchanged"] == 2


def test_format_run_row_columns_align_with_headers():
    sample = {
        "id": 7,
        "root_path": "/srv/share",
        "status": "completed",
        "file_count": 1234,
        "error_count": 5,
        "previous_run_id": None,
        "started_at": "2026-04-26T12:00:00+00:00",
        "completed_at": "2026-04-26T12:34:00+00:00",
    }
    row = format_run_row(sample)
    assert len(row) == len(RUN_COLUMNS)
    assert row[0] == "#7"
    assert row[2] == "completed"
    assert row[3] == "1,234"


def test_format_run_row_marks_rescan_delta():
    sample = {
        "id": 8, "root_path": "/srv/share", "status": "completed",
        "file_count": 1234, "error_count": 0,
        "previous_run_id": 7,
    }
    row = format_run_row(sample)
    assert row[5] == "vs #7"


# ----------------------------------------------------------------------
# Background scan + event posting
# ----------------------------------------------------------------------


def test_start_scan_posts_progress_and_done(tmp_path: Path):
    root = _build_share(tmp_path)
    db = tmp_path / "i.sqlite"

    c = Controller()
    assert c.start_scan(root, db, CrawlOptions(workers=2)) is True

    done = _drain(c, until_kind="done")
    result = done.payload["result"]
    assert done.payload["kind"] == "scan"
    assert result.status == "completed"
    assert result.file_count == 2

    # Controller frees the busy flag once the worker thread returns.
    deadline = time.monotonic() + 2.0
    while c.is_busy() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not c.is_busy()


def test_start_scan_rejects_when_busy(tmp_path: Path):
    """A second start while the first is in flight returns False and
    posts an error event rather than racing against the writer."""
    root = _build_share(tmp_path)
    db1 = tmp_path / "first.sqlite"

    c = Controller()
    assert c.start_scan(root, db1, CrawlOptions(workers=2)) is True
    # Second attempt while the first is running.
    db2 = tmp_path / "second.sqlite"
    second = c.start_scan(root, db2, CrawlOptions(workers=2))
    assert second is False

    # Drain to clean up — the rejection posted its own error event,
    # which we tolerate here.
    _drain(c, until_kind="done", allow_errors=True)


def test_start_rescan_requires_previous_run_id(tmp_path: Path):
    c = Controller()
    ok = c.start_rescan(tmp_path, tmp_path / "i.sqlite",
                         CrawlOptions(workers=1))
    assert ok is False
    ev = c.events.get(timeout=0.5)
    assert ev.kind == "error"
    assert "previous_run_id" in ev.payload["message"]


def test_start_rescan_classifies_added_modified_unchanged(tmp_path: Path):
    root = _build_share(tmp_path)
    db = tmp_path / "i.sqlite"
    base = run_crawl(root, db, CrawlOptions(workers=2))

    (root / "docs" / "c.txt").write_text("gamma")

    c = Controller()
    assert c.start_rescan(
        root, db,
        CrawlOptions(workers=2, previous_run_id=base.run_id),
    ) is True
    done = _drain(c, until_kind="done")
    assert done.payload["kind"] == "rescan"
    sc = done.payload["result"].state_counts
    assert sc["added"] == 1
    assert sc["unchanged"] == 2


def test_start_reports_writes_artifacts_and_posts_event(tmp_path: Path):
    root = _build_share(tmp_path)
    # Make sure at least one file is large enough for the RAG candidate
    # report to emit something.
    (root / "docs" / "long.md").write_text(
        "# Doc\n" + ("content " * 200), encoding="utf-8",
    )
    db = tmp_path / "i.sqlite"
    result = run_crawl(root, db, CrawlOptions(workers=2))

    c = Controller()
    out = tmp_path / "reports"
    assert c.start_reports(db, result.run_id, out) is True
    ev = _drain(c, until_kind="reports")

    assert ev.payload["out_dir"] == str(out)
    artifacts = ev.payload["artifacts"]
    # Six reports, each producing 1+ files; we just sanity-check that
    # something landed.
    assert len(artifacts) >= 6
    assert any(a.endswith(".html") for a in artifacts)
    assert any(a.endswith(".jsonl") for a in artifacts)
    assert (out / "topology.html").exists()


def test_controller_surfaces_unhandled_exceptions_as_error_event(tmp_path: Path):
    """An exception in the worker thread must reach the UI, not be lost."""
    c = Controller()
    # Pass a non-existent root — `run_crawl` itself doesn't fail here
    # (the walker yields a WalkError), so we instead cause failure by
    # making the DB directory unwritable. Easier: trigger it via
    # start_reports against a missing run.
    bogus_db = tmp_path / "missing.sqlite"
    bogus_db.touch()  # exists but empty/invalid for our purposes
    # The schema migration will set it up cleanly, but run_id 999 has
    # no rows in `crawl_runs`. The reports call materialise_folders /
    # query helpers that should at minimum not crash; let's cause a
    # genuine failure with an unwritable out_dir instead.
    out = tmp_path / "reports"
    out.write_text("not a directory")  # collides with mkdir(parents=True)

    # Set up a valid DB + run to scan first.
    root = tmp_path / "share"
    root.mkdir()
    (root / "f.txt").write_text("x")
    db = tmp_path / "ok.sqlite"
    run_crawl(root, db, CrawlOptions(workers=1))
    rid = c.list_runs(db)[0]["id"]

    c.start_reports(db, rid, out)
    ev = _drain(c, until_kind="error")
    assert "message" in ev.payload
