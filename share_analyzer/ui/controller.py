"""Controller — all the cross-thread logic the Tk view needs.

Kept Tk-free so it's testable headlessly. The view just polls
`controller.events` from the Tk main loop via `root.after(...)` and
updates widgets; the controller does the work on background threads.

Event types posted to `events` queue:
  Event("progress", {"files": int, "errors": int, "path": str})
  Event("done",     {"result": CrawlResult, "kind": "scan"|"rescan"})
  Event("reports",  {"out_dir": Path, "artifacts": list[str]})
  Event("error",    {"message": str})
  Event("log",      {"message": str})
"""
from __future__ import annotations

import queue
import sqlite3
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from share_analyzer.crawl.orchestrator import CrawlOptions, CrawlResult, run_crawl
from share_analyzer.index.queries import (
    changed_files_summary, run_summary,
)
from share_analyzer.index.schema import init_db


@dataclass(frozen=True)
class Event:
    kind: str
    payload: dict[str, Any]


class Controller:
    """One-running-task-at-a-time wrapper around `run_crawl` + reports.

    The view never blocks on the controller. Every long operation
    runs in a daemon thread; the controller posts `Event`s to
    `self.events` and the view polls.
    """

    def __init__(self) -> None:
        self.events: queue.Queue[Event] = queue.Queue()
        self._busy = threading.Event()
        self._task: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Status — UI reads these synchronously to enable/disable widgets.
    # ------------------------------------------------------------------

    def is_busy(self) -> bool:
        return self._busy.is_set()

    # ------------------------------------------------------------------
    # Read-only DB helpers — synchronous, fast, no thread.
    # ------------------------------------------------------------------

    def list_runs(self, db_path: Path) -> list[dict]:
        """Return one dict per run, newest first.

        Tolerates a missing DB (returns []) so the view can
        eagerly refresh after a path change without checking.
        """
        if not Path(db_path).exists():
            return []
        try:
            conn = init_db(db_path)
        except sqlite3.DatabaseError:
            return []
        try:
            rows = conn.execute(
                """
                SELECT id, root_path, started_at, completed_at,
                       file_count, error_count, status, previous_run_id
                FROM crawl_runs
                ORDER BY id DESC
                """
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def run_details(self, db_path: Path, run_id: int) -> dict:
        """Full info on one run — for the details panel."""
        conn = init_db(db_path)
        try:
            summary = run_summary(conn, run_id)
            if not summary:
                return {}
            summary = dict(summary)
            try:
                summary["state_counts"] = changed_files_summary(conn, run_id)
            except sqlite3.OperationalError:
                summary["state_counts"] = None
            error_sample = conn.execute(
                "SELECT path, reason FROM crawl_errors "
                " WHERE run_id = ? LIMIT 10",
                (run_id,),
            ).fetchall()
            summary["error_sample"] = [dict(r) for r in error_sample]
            return summary
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Long-running operations — each posts events, sets/clears busy.
    # ------------------------------------------------------------------

    def start_scan(self, root: Path, db_path: Path,
                   options: CrawlOptions) -> bool:
        """Kick off a scan in a daemon thread. Returns False if busy."""
        return self._spawn(self._do_scan, root, db_path, options, kind="scan")

    def start_rescan(self, root: Path, db_path: Path,
                     options: CrawlOptions) -> bool:
        """Kick off a rescan. `options.previous_run_id` must be set."""
        if options.previous_run_id is None:
            self.events.put(Event("error", {
                "message": "rescan requires a previous_run_id",
            }))
            return False
        return self._spawn(self._do_scan, root, db_path, options, kind="rescan")

    def start_reports(self, db_path: Path, run_id: int,
                      out_dir: Path) -> bool:
        """Generate every report into `out_dir`."""
        return self._spawn(self._do_reports, db_path, run_id, out_dir)

    def _spawn(self, fn, *args, **kwargs) -> bool:
        # All start_* calls come from the Tk main thread; daemon threads
        # only ever *clear* `_busy`. So a plain test-and-set is fine —
        # there's no second producer to race against.
        if self._busy.is_set():
            self.events.put(Event("error", {
                "message": "another task is already running",
            }))
            return False
        self._busy.set()
        t = threading.Thread(target=self._run_safely, args=(fn,) + args,
                             kwargs=kwargs, daemon=True)
        self._task = t
        t.start()
        return True

    def _run_safely(self, fn, *args, **kwargs) -> None:
        try:
            fn(*args, **kwargs)
        except BaseException as e:  # noqa: BLE001 — surface in the UI
            self.events.put(Event("error", {
                "message": f"{type(e).__name__}: {e}",
                "trace": traceback.format_exc(),
            }))
        finally:
            self._busy.clear()

    def _do_scan(self, root: Path, db_path: Path,
                 options: CrawlOptions, *, kind: str) -> None:
        def progress_cb(files: int, errors: int, last_path: str) -> None:
            self.events.put(Event("progress", {
                "files": files, "errors": errors, "path": last_path,
            }))
        result: CrawlResult = run_crawl(
            root, db_path, options, progress=progress_cb,
        )
        self.events.put(Event("done", {"result": result, "kind": kind}))

    def _do_reports(self, db_path: Path, run_id: int, out_dir: Path) -> None:
        # Late import — `reports` pulls Plotly which is heavy; only
        # load it when the user actually asks for a report.
        from share_analyzer.reports import run_all
        out_dir.mkdir(parents=True, exist_ok=True)
        conn = init_db(db_path)
        try:
            artifacts = run_all(conn, run_id, out_dir)
        finally:
            conn.close()
        paths: list[str] = []
        for a in artifacts:
            for p in a.paths:
                paths.append(str(p))
        self.events.put(Event("reports", {
            "out_dir": str(out_dir),
            "artifacts": paths,
        }))


def format_run_row(run: dict) -> tuple:
    """Format a `crawl_runs` row for the runs table.

    Centralised so the view's column order matches what tests check.
    """
    delta = ""
    if run.get("previous_run_id"):
        delta = f"vs #{run['previous_run_id']}"
    return (
        f"#{run['id']}",
        run.get("root_path", ""),
        run.get("status", ""),
        f"{run.get('file_count', 0):,}",
        f"{run.get('error_count', 0):,}",
        delta,
        run.get("started_at", "") or "",
        run.get("completed_at", "") or "",
    )


RUN_COLUMNS: tuple[str, ...] = (
    "Run", "Root", "Status", "Files", "Errors", "Delta", "Started", "Completed",
)
