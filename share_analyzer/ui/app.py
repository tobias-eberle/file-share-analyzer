"""Tkinter desktop app for share-analyzer.

The view is deliberately thin: every long operation goes through the
Controller, which posts Events into a queue. The view polls the queue
from Tk's main loop via `after(100, ...)` and updates widgets.

Run with `share-analyzer ui` (from the CLI) or `python -m share_analyzer.ui`.
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import tkinter as tk
import tkinter.filedialog as filedialog
import tkinter.messagebox as messagebox
import tkinter.ttk as ttk
from pathlib import Path
from typing import Optional

from share_analyzer import __version__
from share_analyzer.config import DEFAULT_EXCLUDES
from share_analyzer.crawl.orchestrator import CrawlOptions
from share_analyzer.ui.controller import (
    Controller, Event, RUN_COLUMNS, format_run_row,
)


_POLL_MS = 100


def _open_in_file_manager(path: Path) -> None:
    """Best-effort open of `path` in the OS file manager."""
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError:
        pass


class App:
    def __init__(self) -> None:
        self.controller = Controller()

        self.root = tk.Tk()
        self.root.title(f"Share Analyzer {__version__}")
        self.root.geometry("960x720")
        self.root.minsize(720, 560)

        self.var_path = tk.StringVar()
        self.var_db = tk.StringVar()
        self.var_workers = tk.IntVar(value=8)
        self.var_dir_workers = tk.IntVar(value=4)
        self.var_hash_cap = tk.IntVar(value=100)
        self.var_default_excludes = tk.BooleanVar(value=True)
        self.var_follow_symlinks = tk.BooleanVar(value=False)
        self.var_dry_run = tk.BooleanVar(value=False)
        self.var_subfolder_summary = tk.StringVar(value="All subfolders included.")
        self.var_status = tk.StringVar(value="Idle. Pick a folder and a DB to begin.")
        # Excluded subtrees chosen via the FolderTreeDialog. Cleared
        # whenever the source path changes — selections only make sense
        # against a specific root.
        self._excluded_paths: tuple[str, ...] = ()
        self._excluded_for_path: str = ""
        self.var_path.trace_add("write", lambda *_: self._on_path_changed())

        self._build_layout()
        self._refresh_runs()
        self.root.after(_POLL_MS, self._drain_events)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_layout(self) -> None:
        pad = {"padx": 8, "pady": 4}

        # ---- Inputs --------------------------------------------------
        frm_inputs = ttk.LabelFrame(self.root, text="Source")
        frm_inputs.pack(fill="x", **pad)

        ttk.Label(frm_inputs, text="Folder to scan:").grid(
            row=0, column=0, sticky="e", padx=6, pady=4)
        ttk.Entry(frm_inputs, textvariable=self.var_path, width=60).grid(
            row=0, column=1, sticky="ew", padx=6, pady=4)
        ttk.Button(frm_inputs, text="Browse…",
                   command=self._pick_folder).grid(row=0, column=2, padx=6)

        # Subfolder picker — sits directly under the path entry so the
        # relationship "this set is FOR that path" is obvious.
        ttk.Label(frm_inputs, text="Subfolders:").grid(
            row=1, column=0, sticky="e", padx=6, pady=4)
        sub_row = ttk.Frame(frm_inputs)
        sub_row.grid(row=1, column=1, sticky="ew", padx=6, pady=4)
        ttk.Label(sub_row, textvariable=self.var_subfolder_summary,
                  foreground="#444").pack(side="left")
        ttk.Button(sub_row, text="Reset",
                   command=self._reset_subfolders).pack(side="right",
                                                         padx=(6, 0))
        ttk.Button(frm_inputs, text="Choose…",
                   command=self._pick_subfolders).grid(
            row=1, column=2, padx=6)

        ttk.Label(frm_inputs, text="Index DB:").grid(
            row=2, column=0, sticky="e", padx=6, pady=4)
        ttk.Entry(frm_inputs, textvariable=self.var_db, width=60).grid(
            row=2, column=1, sticky="ew", padx=6, pady=4)
        btn_db = ttk.Frame(frm_inputs)
        btn_db.grid(row=2, column=2, padx=6)
        ttk.Button(btn_db, text="Open…",
                   command=self._pick_existing_db).pack(side="left")
        ttk.Button(btn_db, text="New…",
                   command=self._pick_new_db).pack(side="left", padx=(4, 0))
        frm_inputs.columnconfigure(1, weight=1)

        # ---- Options -------------------------------------------------
        frm_opts = ttk.LabelFrame(self.root, text="Options")
        frm_opts.pack(fill="x", **pad)

        ttk.Label(frm_opts, text="Workers:").grid(row=0, column=0, padx=6, pady=4)
        ttk.Spinbox(frm_opts, from_=1, to=64, width=5,
                    textvariable=self.var_workers).grid(row=0, column=1, padx=6)

        ttk.Label(frm_opts, text="Dir workers:").grid(row=0, column=2, padx=6)
        ttk.Spinbox(frm_opts, from_=1, to=32, width=5,
                    textvariable=self.var_dir_workers).grid(row=0, column=3, padx=6)

        ttk.Label(frm_opts, text="Hash cap MB:").grid(row=0, column=4, padx=6)
        ttk.Spinbox(frm_opts, from_=1, to=10_000, width=6,
                    textvariable=self.var_hash_cap).grid(row=0, column=5, padx=6)

        ttk.Checkbutton(frm_opts, text="Default excludes",
                        variable=self.var_default_excludes).grid(
            row=1, column=0, columnspan=2, sticky="w", padx=6, pady=4)
        ttk.Checkbutton(frm_opts, text="Follow symlinks",
                        variable=self.var_follow_symlinks).grid(
            row=1, column=2, columnspan=2, sticky="w", padx=6, pady=4)
        ttk.Checkbutton(frm_opts, text="Dry-run (count only, no DB write)",
                        variable=self.var_dry_run).grid(
            row=1, column=4, columnspan=2, sticky="w", padx=6, pady=4)

        # ---- Actions + progress -------------------------------------
        frm_run = ttk.Frame(self.root)
        frm_run.pack(fill="x", **pad)

        self.btn_scan = ttk.Button(frm_run, text="Start scan",
                                    command=self._on_start_scan)
        self.btn_scan.pack(side="left")

        self.var_progress = tk.StringVar(value="")
        ttk.Label(frm_run, textvariable=self.var_progress,
                  foreground="#444", font=("TkFixedFont", 10)).pack(
            side="left", padx=12, fill="x", expand=True)

        self.progress = ttk.Progressbar(frm_run, mode="indeterminate", length=120)
        self.progress.pack(side="right")

        # ---- Runs table ---------------------------------------------
        frm_runs = ttk.LabelFrame(self.root, text="Runs in this DB")
        frm_runs.pack(fill="both", expand=True, **pad)

        cols = RUN_COLUMNS
        self.tree = ttk.Treeview(frm_runs, columns=cols, show="headings",
                                  height=8)
        for c in cols:
            self.tree.heading(c, text=c)
            self.tree.column(c, width=110, anchor="w")
        self.tree.column("Run", width=60)
        self.tree.column("Files", width=80, anchor="e")
        self.tree.column("Errors", width=70, anchor="e")
        self.tree.column("Root", width=240)

        sb = ttk.Scrollbar(frm_runs, orient="vertical",
                            command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select_run)

        # ---- Actions for a selected run -----------------------------
        frm_run_actions = ttk.Frame(self.root)
        frm_run_actions.pack(fill="x", **pad)

        self.btn_refresh = ttk.Button(frm_run_actions, text="Refresh",
                                       command=self._refresh_runs)
        self.btn_refresh.pack(side="left")
        self.btn_rescan = ttk.Button(frm_run_actions, text="Rescan against this run",
                                      command=self._on_rescan, state="disabled")
        self.btn_rescan.pack(side="left", padx=(8, 0))
        self.btn_reports = ttk.Button(frm_run_actions, text="Generate reports…",
                                       command=self._on_reports, state="disabled")
        self.btn_reports.pack(side="left", padx=(8, 0))
        self.btn_info = ttk.Button(frm_run_actions, text="Show details",
                                    command=self._on_info, state="disabled")
        self.btn_info.pack(side="left", padx=(8, 0))

        # ---- Status bar ---------------------------------------------
        ttk.Separator(self.root, orient="horizontal").pack(fill="x")
        ttk.Label(self.root, textvariable=self.var_status, anchor="w",
                  padding=(8, 4)).pack(fill="x")

    # ------------------------------------------------------------------
    # Folder + DB pickers
    # ------------------------------------------------------------------

    def _pick_folder(self) -> None:
        chosen = filedialog.askdirectory(
            title="Choose folder to scan",
            mustexist=True,
        )
        if chosen:
            self.var_path.set(chosen)

    def _pick_existing_db(self) -> None:
        chosen = filedialog.askopenfilename(
            title="Open existing index",
            filetypes=[("SQLite database", "*.sqlite *.db"),
                       ("All files", "*.*")],
        )
        if chosen:
            self.var_db.set(chosen)
            self._refresh_runs()

    def _pick_new_db(self) -> None:
        chosen = filedialog.asksaveasfilename(
            title="Create / overwrite index",
            defaultextension=".sqlite",
            filetypes=[("SQLite database", "*.sqlite"),
                       ("All files", "*.*")],
        )
        if chosen:
            self.var_db.set(chosen)
            self._refresh_runs()

    # ------------------------------------------------------------------
    # Subfolder selection
    # ------------------------------------------------------------------

    def _pick_subfolders(self) -> None:
        path = (self.var_path.get() or "").strip()
        if not path or not Path(path).is_dir():
            messagebox.showerror(
                "share-analyzer",
                "Pick a 'Folder to scan' first — subfolder selection is "
                "relative to that root.",
            )
            return
        # Late import — keeps the FolderTreeDialog import out of the
        # cold-start path for users who only ever use the CLI.
        from share_analyzer.ui.folder_tree import FolderTreeDialog
        # Carry forward existing selection if the user is re-opening the
        # dialog for the same root.
        initial = self._excluded_paths if self._excluded_for_path == path else ()
        result = FolderTreeDialog(
            self.root, path, initial_excluded=initial,
        ).show()
        if result is None:
            return  # cancelled
        self._excluded_paths = result
        self._excluded_for_path = path
        self._refresh_subfolder_summary()

    def _reset_subfolders(self) -> None:
        self._excluded_paths = ()
        self._excluded_for_path = ""
        self._refresh_subfolder_summary()

    def _on_path_changed(self) -> None:
        # If the user typed a different root, the prior excluded set is
        # against the wrong tree. Clearing is the safe default.
        current = (self.var_path.get() or "").strip()
        if current != self._excluded_for_path and self._excluded_paths:
            self._excluded_paths = ()
            self._excluded_for_path = ""
        self._refresh_subfolder_summary()

    def _refresh_subfolder_summary(self) -> None:
        n = len(self._excluded_paths)
        if n == 0:
            self.var_subfolder_summary.set("All subfolders included.")
        else:
            self.var_subfolder_summary.set(
                f"{n} subtree(s) excluded."
            )

    # ------------------------------------------------------------------
    # Run actions
    # ------------------------------------------------------------------

    def _resolve_options(self) -> CrawlOptions:
        excludes = DEFAULT_EXCLUDES if self.var_default_excludes.get() else ()
        # Only pass the excluded set if it was chosen against the
        # current path — `_on_path_changed` already clears stale ones,
        # but be defensive in case the field was edited concurrently.
        current_path = (self.var_path.get() or "").strip()
        excluded_paths = (
            self._excluded_paths
            if self._excluded_for_path == current_path else ()
        )
        return CrawlOptions(
            workers=max(1, int(self.var_workers.get() or 1)),
            dir_workers=max(1, int(self.var_dir_workers.get() or 1)),
            hash_cap_bytes=int(self.var_hash_cap.get() or 1) * 1024 * 1024,
            exclude_globs=excludes,
            excluded_paths=excluded_paths,
            follow_symlinks=bool(self.var_follow_symlinks.get()),
        )

    def _on_start_scan(self) -> None:
        path = (self.var_path.get() or "").strip()
        if not path or not Path(path).is_dir():
            messagebox.showerror("share-analyzer",
                                 "Pick an existing folder to scan.")
            return

        if self.var_dry_run.get():
            self._run_dry_run(Path(path))
            return

        db = (self.var_db.get() or "").strip()
        if not db:
            messagebox.showerror("share-analyzer",
                                 "Pick or create an index DB file first.")
            return

        options = self._resolve_options()
        self._set_running(f"Scanning {path}…")
        if not self.controller.start_scan(Path(path), Path(db), options):
            self._set_idle()
            messagebox.showerror("share-analyzer",
                                 "Another task is already running.")

    def _run_dry_run(self, path: Path) -> None:
        # The dry-run path is fast and synchronous — no DB writes, just
        # walk + count. Run it inline rather than spinning a thread.
        from share_analyzer.dry_run import dry_run, format_summary
        excluded_paths = (
            self._excluded_paths
            if self._excluded_for_path == str(path) else ()
        )
        try:
            summary = dry_run(
                path,
                exclude_globs=DEFAULT_EXCLUDES if self.var_default_excludes.get() else (),
                excluded_paths=excluded_paths,
                follow_symlinks=self.var_follow_symlinks.get(),
                dir_workers=max(1, int(self.var_dir_workers.get() or 1)),
            )
        except Exception as e:  # noqa: BLE001 — show the user
            messagebox.showerror("share-analyzer", f"dry-run failed: {e}")
            return
        messagebox.showinfo("Dry-run summary", format_summary(summary))

    def _selected_run_id(self) -> Optional[int]:
        sel = self.tree.selection()
        if not sel:
            return None
        try:
            return int(self.tree.item(sel[0], "values")[0].lstrip("#"))
        except (IndexError, ValueError):
            return None

    def _on_rescan(self) -> None:
        run_id = self._selected_run_id()
        if run_id is None:
            return
        db = (self.var_db.get() or "").strip()
        if not db:
            return
        details = self.controller.run_details(Path(db), run_id)
        prev_root = details.get("root_path")
        if not prev_root:
            messagebox.showerror("share-analyzer",
                                 "Couldn't read the prior run's root path.")
            return
        if details.get("status") != "completed":
            messagebox.showerror(
                "share-analyzer",
                f"Run #{run_id} status is '{details.get('status')}', "
                "rescan refuses to diff against a non-completed run.")
            return
        if not Path(prev_root).is_dir():
            messagebox.showerror(
                "share-analyzer",
                f"Prior run's root '{prev_root}' is no longer accessible.")
            return

        options = self._resolve_options()
        # Force the scan path to match the prior root — rescan needs them
        # to align so file paths line up across runs.
        self.var_path.set(prev_root)
        options.previous_run_id = run_id  # type: ignore[misc]
        self._set_running(f"Rescanning {prev_root} against run #{run_id}…")
        if not self.controller.start_rescan(
            Path(prev_root), Path(db), options,
        ):
            self._set_idle()

    def _on_reports(self) -> None:
        run_id = self._selected_run_id()
        if run_id is None:
            return
        db = (self.var_db.get() or "").strip()
        if not db:
            return
        out = filedialog.askdirectory(
            title=f"Output folder for reports of run #{run_id}",
        )
        if not out:
            return
        self._set_running(f"Generating reports for run #{run_id}…")
        if not self.controller.start_reports(Path(db), run_id, Path(out)):
            self._set_idle()

    def _on_info(self) -> None:
        run_id = self._selected_run_id()
        if run_id is None:
            return
        db = (self.var_db.get() or "").strip()
        if not db:
            return
        d = self.controller.run_details(Path(db), run_id)
        if not d:
            messagebox.showerror("share-analyzer",
                                 f"Run #{run_id} not found.")
            return
        lines = [
            f"Run #{d.get('id')}",
            f"  Root      : {d.get('root_path')}",
            f"  Status    : {d.get('status')}",
            f"  Started   : {d.get('started_at')}",
            f"  Completed : {d.get('completed_at')}",
            f"  Files     : {d.get('file_count', 0):,}",
            f"  Errors    : {d.get('error_count', 0):,}",
            f"  Workers   : {d.get('workers')}",
            f"  Hash cap  : {d.get('hash_cap_bytes')} bytes",
        ]
        sc = d.get("state_counts")
        if sc and any(v for k, v in sc.items() if k != "baseline"):
            lines.append(
                f"  Delta     : +{sc.get('added', 0):,} added "
                f"~{sc.get('modified', 0):,} modified "
                f"={sc.get('unchanged', 0):,} unchanged "
                f"-{sc.get('deleted', 0):,} deleted"
            )
        sample = d.get("error_sample") or []
        if sample:
            lines.append("  Errors:")
            for e in sample:
                lines.append(f"    • {e['path']}: {e['reason']}")
        messagebox.showinfo(f"Run #{run_id}", "\n".join(lines))

    def _on_select_run(self, _event=None) -> None:
        has_selection = bool(self.tree.selection())
        state = "normal" if has_selection else "disabled"
        for btn in (self.btn_rescan, self.btn_reports, self.btn_info):
            btn.configure(state=state)

    # ------------------------------------------------------------------
    # Runs table refresh
    # ------------------------------------------------------------------

    def _refresh_runs(self) -> None:
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        db = (self.var_db.get() or "").strip()
        if not db:
            return
        runs = self.controller.list_runs(Path(db))
        for run in runs:
            self.tree.insert("", "end", values=format_run_row(run))
        self._on_select_run()

    # ------------------------------------------------------------------
    # Event polling
    # ------------------------------------------------------------------

    def _drain_events(self) -> None:
        try:
            while True:
                ev = self.controller.events.get_nowait()
                self._handle(ev)
        except queue.Empty:
            pass
        self.root.after(_POLL_MS, self._drain_events)

    def _handle(self, ev: Event) -> None:
        if ev.kind == "progress":
            files = ev.payload.get("files", 0)
            errors = ev.payload.get("errors", 0)
            path = ev.payload.get("path") or ""
            if len(path) > 70:
                path = "…" + path[-69:]
            self.var_progress.set(
                f"{files:>10,} files   {errors:>4,} err   {path}"
            )
        elif ev.kind == "done":
            result = ev.payload["result"]
            kind = ev.payload.get("kind", "scan")
            self._set_idle()
            self._refresh_runs()
            if result.status == "disconnected":
                msg = (f"{kind.title()} interrupted: run #{result.run_id} "
                       f"marked 'disconnected' "
                       f"({result.error_count:,} errors).")
                if result.advisory:
                    msg += f"\n\nAdvisory: {result.advisory}"
                messagebox.showwarning("share-analyzer", msg)
                return
            sc = result.state_counts or {}
            if sc:
                msg = (f"Rescan #{result.run_id} done: "
                       f"+{sc.get('added', 0):,} added, "
                       f"~{sc.get('modified', 0):,} modified, "
                       f"={sc.get('unchanged', 0):,} unchanged, "
                       f"-{sc.get('deleted', 0):,} deleted "
                       f"({result.error_count:,} errors).")
            else:
                msg = (f"Scan #{result.run_id} done: "
                       f"{result.file_count:,} files, "
                       f"{result.error_count:,} errors.")
            if result.advisory:
                msg += f"\n\nAdvisory: {result.advisory}"
            self.var_status.set(msg.replace("\n\n", " — "))
        elif ev.kind == "reports":
            out = ev.payload.get("out_dir") or ""
            artifacts = ev.payload.get("artifacts") or []
            self._set_idle()
            self.var_status.set(
                f"Reports generated: {len(artifacts)} files in {out}"
            )
            if out and messagebox.askyesno(
                "share-analyzer",
                f"Reports written to:\n{out}\n\nOpen the folder?",
            ):
                _open_in_file_manager(Path(out))
        elif ev.kind == "error":
            self._set_idle()
            messagebox.showerror("share-analyzer",
                                  ev.payload.get("message", "unknown error"))

    # ------------------------------------------------------------------
    # Running-state toggle
    # ------------------------------------------------------------------

    def _set_running(self, status: str) -> None:
        self.var_status.set(status)
        self.btn_scan.configure(state="disabled")
        self.btn_rescan.configure(state="disabled")
        self.btn_reports.configure(state="disabled")
        self.btn_info.configure(state="disabled")
        self.btn_refresh.configure(state="disabled")
        self.progress.start(50)

    def _set_idle(self) -> None:
        self.btn_scan.configure(state="normal")
        self.btn_refresh.configure(state="normal")
        self.progress.stop()
        self._on_select_run()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    App().run()


if __name__ == "__main__":
    main()
