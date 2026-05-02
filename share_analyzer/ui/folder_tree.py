"""Tk dialog for picking subfolders to include in a scan.

A modal `Toplevel` window with a `ttk.Treeview` that lazy-loads
children on expand. Each row shows the folder name plus a state
column (`✓` included, `✗` excluded, `◐` mixed). Toggle by
double-click, by pressing Space, or via the Include/Exclude buttons.

The actual selection logic lives in `folder_selection.FolderSelection`
— this file is just the view. On OK, returns the list of excluded
paths the walker should skip.
"""
from __future__ import annotations

import tkinter as tk
import tkinter.ttk as ttk
from pathlib import Path
from typing import Optional

from share_analyzer.ui.folder_selection import (
    EXCLUDED, INCLUDED, FolderSelection, list_subfolders,
)


_INCLUDED_GLYPH = "✓"
_EXCLUDED_GLYPH = "✗"
_MIXED_GLYPH = "◐"
_PLACEHOLDER_TEXT = "…loading…"


class FolderTreeDialog:
    """Modal subfolder picker.

    Usage:
        dlg = FolderTreeDialog(parent, root="/srv/share")
        excluded = dlg.show()  # returns tuple[str, ...] or None on cancel
    """

    def __init__(self, parent: tk.Misc, root: str | Path,
                 *, initial_excluded: tuple[str, ...] = ()) -> None:
        self.selection = FolderSelection(root)
        for p in initial_excluded:
            self.selection.set_state(p, EXCLUDED)

        self.top = tk.Toplevel(parent)
        self.top.title("Choose subfolders to include")
        self.top.geometry("640x520")
        self.top.transient(parent)
        self.top.grab_set()

        self._result: Optional[tuple[str, ...]] = None
        # iid (Treeview id) ↔ absolute path
        self._iid_to_path: dict[str, str] = {}
        self._path_to_iid: dict[str, str] = {}
        # iids whose children have already been populated for real
        self._populated: set[str] = set()

        self._build_layout()
        self._populate_root()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_layout(self) -> None:
        frame = ttk.Frame(self.top, padding=8)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame,
            text=(
                "Double-click or press Space to toggle a folder. "
                "Excluded folders (✗) and everything under them are "
                "skipped during the scan."
            ),
            wraplength=600,
            foreground="#444",
        ).pack(anchor="w", pady=(0, 6))

        self.tree = ttk.Treeview(
            frame, columns=("state",), show="tree headings",
            selectmode="extended",
        )
        self.tree.heading("#0", text="Folder")
        self.tree.heading("state", text="")
        self.tree.column("#0", width=460)
        self.tree.column("state", width=40, anchor="center", stretch=False)

        self.tree.tag_configure("excluded", foreground="#aaa")

        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.tree.bind("<<TreeviewOpen>>", self._on_expand)
        self.tree.bind("<Double-Button-1>", self._on_double_click)
        self.tree.bind("<space>", self._on_space)

        # Action buttons
        actions = ttk.Frame(self.top, padding=(8, 4))
        actions.pack(fill="x")
        ttk.Button(actions, text="Include selected",
                   command=lambda: self._apply(INCLUDED)).pack(side="left")
        ttk.Button(actions, text="Exclude selected",
                   command=lambda: self._apply(EXCLUDED)).pack(side="left",
                                                                padx=(6, 0))

        ttk.Separator(self.top, orient="horizontal").pack(fill="x")
        bottom = ttk.Frame(self.top, padding=8)
        bottom.pack(fill="x")
        self.var_summary = tk.StringVar(value="")
        ttk.Label(bottom, textvariable=self.var_summary,
                  foreground="#444").pack(side="left")
        ttk.Button(bottom, text="Cancel",
                   command=self._on_cancel).pack(side="right")
        ttk.Button(bottom, text="Use selection",
                   command=self._on_ok).pack(side="right", padx=(0, 6))

    # ------------------------------------------------------------------
    # Populating the tree
    # ------------------------------------------------------------------

    def _populate_root(self) -> None:
        root_iid = self.tree.insert(
            "", "end", text=str(self.selection.root),
            values=(_INCLUDED_GLYPH,), open=True,
        )
        self._iid_to_path[root_iid] = self.selection.root
        self._path_to_iid[self.selection.root] = root_iid
        self._add_children(root_iid, self.selection.root)
        self._populated.add(root_iid)
        self._refresh_glyph(root_iid)
        self._update_summary()

    def _add_children(self, parent_iid: str, parent_path: str) -> None:
        for child_path in list_subfolders(parent_path):
            self.selection.remember(child_path)
            iid = self.tree.insert(
                parent_iid, "end", text=Path(child_path).name,
                values=(_INCLUDED_GLYPH,),
            )
            self._iid_to_path[iid] = child_path
            self._path_to_iid[child_path] = iid
            # Placeholder so the disclosure arrow renders before we
            # actually scandir this folder's children.
            self.tree.insert(iid, "end", text=_PLACEHOLDER_TEXT)
            self._refresh_glyph(iid)

    def _on_expand(self, _event=None) -> None:
        iid = self.tree.focus()
        if not iid or iid in self._populated:
            return
        # Drop placeholder, real-populate.
        for child in self.tree.get_children(iid):
            self.tree.delete(child)
        path = self._iid_to_path.get(iid)
        if path is not None:
            self._add_children(iid, path)
        self._populated.add(iid)

    # ------------------------------------------------------------------
    # Toggling state
    # ------------------------------------------------------------------

    def _on_double_click(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
            self._toggle([iid])

    def _on_space(self, _event=None) -> str:
        self._toggle(list(self.tree.selection()))
        return "break"  # don't let Space scroll the tree

    def _apply(self, state: str) -> None:
        for iid in self.tree.selection():
            path = self._iid_to_path.get(iid)
            if path is None or path == self.selection.root:
                continue
            self.selection.set_state(path, state)
        self._refresh_all_glyphs()
        self._update_summary()

    def _toggle(self, iids: list[str]) -> None:
        if not iids:
            return
        for iid in iids:
            path = self._iid_to_path.get(iid)
            if path is None or path == self.selection.root:
                # Toggling the root would mean "scan nothing", which is
                # never what the user means here. Silently ignore.
                continue
            self.selection.toggle(path)
        self._refresh_all_glyphs()
        self._update_summary()

    # ------------------------------------------------------------------
    # Glyph rendering
    # ------------------------------------------------------------------

    def _glyph_for(self, path: str) -> str:
        state = self.selection.state_of(path)
        if state == EXCLUDED:
            return _EXCLUDED_GLYPH
        if self.selection.has_excluded_descendants(path):
            return _MIXED_GLYPH
        return _INCLUDED_GLYPH

    def _refresh_glyph(self, iid: str) -> None:
        path = self._iid_to_path.get(iid)
        if path is None:
            return
        self.tree.set(iid, "state", self._glyph_for(path))
        if self.selection.is_effectively_excluded(path):
            self.tree.item(iid, tags=("excluded",))
        else:
            self.tree.item(iid, tags=())

    def _refresh_all_glyphs(self) -> None:
        for iid in self._iid_to_path:
            self._refresh_glyph(iid)

    def _update_summary(self) -> None:
        excluded = self.selection.excluded_paths()
        if not excluded:
            self.var_summary.set("All subfolders included.")
        else:
            self.var_summary.set(
                f"{len(excluded)} subtree(s) excluded."
            )

    # ------------------------------------------------------------------
    # Buttons / lifecycle
    # ------------------------------------------------------------------

    def _on_ok(self) -> None:
        self._result = self.selection.excluded_paths()
        self.top.destroy()

    def _on_cancel(self) -> None:
        self._result = None
        self.top.destroy()

    def show(self) -> Optional[tuple[str, ...]]:
        self.top.wait_window()
        return self._result
