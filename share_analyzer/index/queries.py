"""Aggregation queries used by the report layer.

Reports never touch the filesystem; they only read from these helpers.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional


def latest_run_id(conn: sqlite3.Connection) -> Optional[int]:
    row = conn.execute(
        "SELECT id FROM crawl_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def run_summary(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id, root_path, started_at, completed_at,
               file_count, error_count, status, workers, hash_cap_bytes
        FROM crawl_runs WHERE id = ?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        return {}
    return dict(row)


def _folder_parent(path: str, root: str) -> Optional[str]:
    if path == root:
        return None
    norm = path.rstrip("/\\")
    for sep in ("/", "\\"):
        idx = norm.rfind(sep)
        if idx >= 0:
            parent = norm[:idx] or sep
            return parent
    return None


def _folder_depth(path: str, root: str) -> int:
    if path == root:
        return 0
    rel = path[len(root):].strip("/\\")
    if not rel:
        return 0
    return rel.replace("\\", "/").count("/") + 1


def materialize_folders(conn: sqlite3.Connection, run_id: int) -> int:
    """Compute folder aggregates from `files` and write them into `folders`.

    Called once at the end of a crawl, then read by every report.

    Three guarantees the reports rely on:
      1. Every ancestor of a file-bearing folder is in the table, even when
         that ancestor contains no direct files (so `Projects/` shows up
         in the topology even if all its files live in subdirectories).
      2. `total_size` and `file_count` are recursive — a folder's size is
         the sum of its direct files plus the rolled-up size of every
         descendant.
      3. `max_depth_below` is the depth of the deepest descendant relative
         to the folder itself.

    Dominant MIME stays direct-files-only by design: a recursive rollup
    of categories is more confusing than useful at a glance.
    """
    run = conn.execute(
        "SELECT root_path FROM crawl_runs WHERE id = ?", (run_id,)
    ).fetchone()
    if run is None:
        return 0
    root = run["root_path"]

    conn.execute("DELETE FROM folders WHERE run_id = ?", (run_id,))

    direct: dict[str, dict] = {}
    for r in conn.execute(
        """
        SELECT parent_path AS path,
               COUNT(*)    AS file_count,
               SUM(size)   AS total_size,
               MIN(mtime)  AS mtime_min,
               MAX(mtime)  AS mtime_max
        FROM files
        WHERE run_id = ? AND state != 'deleted'
        GROUP BY parent_path
        """,
        (run_id,),
    ):
        direct[r["path"]] = {
            "file_count": r["file_count"],
            "total_size": r["total_size"] or 0,
            "mtime_min": r["mtime_min"],
            "mtime_max": r["mtime_max"],
        }

    dominant: dict[str, str] = {}
    for r in conn.execute(
        """
        SELECT parent_path, mime_category, SUM(size) AS s
        FROM files
        WHERE run_id = ? AND state != 'deleted' AND mime_category IS NOT NULL
        GROUP BY parent_path, mime_category
        ORDER BY parent_path, s DESC
        """,
        (run_id,),
    ):
        dominant.setdefault(r["parent_path"], r["mime_category"])

    folders: dict[str, dict] = {}

    def _ensure(path: str) -> None:
        if path in folders:
            return
        d = direct.get(path)
        folders[path] = {
            "file_count":      d["file_count"]  if d else 0,
            "total_size":      d["total_size"]  if d else 0,
            "mtime_min":       d["mtime_min"]   if d else None,
            "mtime_max":       d["mtime_max"]   if d else None,
            "max_depth_below": 0,
        }

    for path in direct:
        cur: Optional[str] = path
        while cur is not None:
            if cur in folders:
                break
            _ensure(cur)
            if cur == root:
                break
            cur = _folder_parent(cur, root)
    _ensure(root)

    def _merge_mtime(parent: dict, child_min: Optional[str], child_max: Optional[str]) -> None:
        if child_min and (parent["mtime_min"] is None or child_min < parent["mtime_min"]):
            parent["mtime_min"] = child_min
        if child_max and (parent["mtime_max"] is None or child_max > parent["mtime_max"]):
            parent["mtime_max"] = child_max

    by_depth_desc = sorted(
        folders, key=lambda p: _folder_depth(p, root), reverse=True
    )
    for path in by_depth_desc:
        if path == root:
            continue
        parent = _folder_parent(path, root)
        if parent is None or parent not in folders:
            continue
        f = folders[path]
        p = folders[parent]
        p["file_count"] += f["file_count"]
        p["total_size"] += f["total_size"]
        _merge_mtime(p, f["mtime_min"], f["mtime_max"])
        depth_diff = _folder_depth(path, root) - _folder_depth(parent, root)
        candidate = depth_diff + f["max_depth_below"]
        if candidate > p["max_depth_below"]:
            p["max_depth_below"] = candidate

    final_rows = [
        (
            run_id, path,
            _folder_parent(path, root),
            _folder_depth(path, root),
            f["file_count"], f["total_size"], f["max_depth_below"],
            f["mtime_min"], f["mtime_max"],
            dominant.get(path),
        )
        for path, f in folders.items()
    ]
    conn.executemany(
        """
        INSERT INTO folders (
            run_id, path, parent_path, depth, file_count, total_size,
            max_depth_below, mtime_min, mtime_max, dominant_mime_category
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        final_rows,
    )
    return len(final_rows)


def topology(conn: sqlite3.Connection, run_id: int, max_depth: int = 4) -> list[dict]:
    rows = conn.execute(
        """
        SELECT path, parent_path, depth, file_count, total_size,
               mtime_max, dominant_mime_category
        FROM folders
        WHERE run_id = ? AND depth <= ?
        ORDER BY total_size DESC
        """,
        (run_id, max_depth),
    ).fetchall()
    return [dict(r) for r in rows]


def size_hotspots(conn: sqlite3.Connection, run_id: int,
                  level: int, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """
        SELECT path, file_count, total_size, dominant_mime_category, mtime_max
        FROM folders
        WHERE run_id = ? AND depth = ?
        ORDER BY total_size DESC
        LIMIT ?
        """,
        (run_id, level, limit),
    ).fetchall()
    return [dict(r) for r in rows]


_STALENESS_ORDER = ("<1y", "1-3y", "3-5y", "5y+")


def staleness_buckets(conn: sqlite3.Connection, run_id: int,
                       now: Optional[datetime] = None) -> list[dict]:
    """Bucket files by mtime age.

    Pure-SQL aggregation — `mtime` is stored as ISO-8601 UTC, which is
    lexicographically sortable, so the cutoffs become string compares.
    """
    now = now or datetime.now(timezone.utc)
    c1 = (now - timedelta(days=365)).isoformat()
    c3 = (now - timedelta(days=365 * 3)).isoformat()
    c5 = (now - timedelta(days=365 * 5)).isoformat()

    rows = conn.execute(
        """
        SELECT bucket,
               SUM(c)  AS file_count,
               SUM(s)  AS total_size
        FROM (
            SELECT
                CASE
                    WHEN mtime >= ? THEN '<1y'
                    WHEN mtime >= ? THEN '1-3y'
                    WHEN mtime >= ? THEN '3-5y'
                    ELSE '5y+'
                END AS bucket,
                1 AS c,
                COALESCE(size, 0) AS s
            FROM files
            WHERE run_id = ? AND state != 'deleted' AND mtime IS NOT NULL
        )
        GROUP BY bucket
        """,
        (c1, c3, c5, run_id),
    ).fetchall()
    by_bucket = {r["bucket"]: dict(r) for r in rows}
    return [
        {
            "bucket": k,
            "file_count": (by_bucket.get(k) or {}).get("file_count", 0) or 0,
            "total_size": (by_bucket.get(k) or {}).get("total_size", 0) or 0,
        }
        for k in _STALENESS_ORDER
    ]


# Sample-path separator: ASCII Unit Separator never appears in filesystem paths.
_SAMPLE_SEP = "\x1f"


def top_duplicates(conn: sqlite3.Connection, run_id: int,
                   limit: int = 100, sample_size: int = 5) -> list[dict]:
    """Top duplicate clusters with sample paths, in a single SQL query."""
    rows = conn.execute(
        f"""
        WITH ranked AS (
            SELECT
                sha256, path, size,
                ROW_NUMBER() OVER (PARTITION BY sha256 ORDER BY path) AS rn,
                COUNT(*)    OVER (PARTITION BY sha256)              AS file_count
            FROM files
            WHERE run_id = ? AND state != 'deleted' AND sha256 IS NOT NULL
        ),
        clusters AS (
            SELECT
                sha256,
                file_count,
                MAX(size)                  AS file_size,
                (file_count - 1) * MAX(size) AS wasted_bytes,
                GROUP_CONCAT(
                    CASE WHEN rn <= ? THEN path END,
                    '{_SAMPLE_SEP}'
                ) AS sample_paths_concat
            FROM ranked
            GROUP BY sha256
            HAVING file_count >= 2
        )
        SELECT * FROM clusters
        ORDER BY wasted_bytes DESC
        LIMIT ?
        """,
        (run_id, sample_size, limit),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        concat = d.pop("sample_paths_concat") or ""
        d["sample_paths"] = [p for p in concat.split(_SAMPLE_SEP) if p]
        out.append(d)
    return out


def category_distribution(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT COALESCE(mime_category, 'other') AS category,
               COUNT(*) AS file_count,
               SUM(size) AS total_size
        FROM files
        WHERE run_id = ? AND state != 'deleted'
        GROUP BY category
        ORDER BY total_size DESC
        """,
        (run_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def extension_distribution(conn: sqlite3.Connection, run_id: int,
                            limit: int = 20) -> list[dict]:
    rows = conn.execute(
        """
        SELECT COALESCE(NULLIF(extension, ''), '(none)') AS extension,
               COUNT(*) AS file_count,
               SUM(size) AS total_size
        FROM files
        WHERE run_id = ? AND state != 'deleted'
        GROUP BY extension
        ORDER BY file_count DESC
        LIMIT ?
        """,
        (run_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def rag_candidates(conn: sqlite3.Connection, run_id: int, *,
                   min_size: int, max_size: int,
                   max_age_days: int,
                   categories: tuple[str, ...] = ("text-extractable",),
                   include_one_per_dup: bool = True,
                   now: Optional[datetime] = None):
    """Yield RAG candidate rows.

    Dedup is done in SQL via `ROW_NUMBER() OVER (PARTITION BY sha256)`.
    Files without a sha256 (oversized) get a synthetic per-path partition
    so they're never collapsed against unrelated files.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=max_age_days)).isoformat()
    placeholders = ",".join("?" for _ in categories)
    if include_one_per_dup:
        sql = f"""
            WITH filtered AS (
                SELECT id, path, parent_path, name, extension, size, mtime,
                       sha256, mime_type, mime_category,
                       ROW_NUMBER() OVER (
                           PARTITION BY COALESCE(sha256, 'p:' || path)
                           ORDER BY mtime DESC, id ASC
                       ) AS rn
                FROM files
                WHERE run_id = ?
                  AND state != 'deleted'
                  AND mime_category IN ({placeholders})
                  AND size BETWEEN ? AND ?
                  AND (mtime IS NULL OR mtime >= ?)
            )
            SELECT id, path, parent_path, name, extension, size, mtime,
                   sha256, mime_type, mime_category
            FROM filtered
            WHERE rn = 1
            ORDER BY mtime DESC
        """
    else:
        sql = f"""
            SELECT id, path, parent_path, name, extension, size, mtime,
                   sha256, mime_type, mime_category
            FROM files
            WHERE run_id = ?
              AND state != 'deleted'
              AND mime_category IN ({placeholders})
              AND size BETWEEN ? AND ?
              AND (mtime IS NULL OR mtime >= ?)
            ORDER BY mtime DESC
        """
    params = [run_id, *categories, min_size, max_size, cutoff]
    for row in conn.execute(sql, params):
        yield dict(row)


def changed_files_summary(conn: sqlite3.Connection, run_id: int) -> dict[str, int]:
    """Per-state counts for an incremental run. Returns 0s for missing states."""
    rows = conn.execute(
        """
        SELECT state, COUNT(*) AS n
        FROM files
        WHERE run_id = ?
        GROUP BY state
        """,
        (run_id,),
    ).fetchall()
    counts = {"baseline": 0, "added": 0, "modified": 0, "unchanged": 0, "deleted": 0}
    for r in rows:
        counts[r["state"]] = r["n"]
    return counts
