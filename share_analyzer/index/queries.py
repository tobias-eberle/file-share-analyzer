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
    Done in Python to keep parent/depth logic portable across separators.
    """
    run = conn.execute(
        "SELECT root_path FROM crawl_runs WHERE id = ?", (run_id,)
    ).fetchone()
    if run is None:
        return 0
    root = run["root_path"]

    conn.execute("DELETE FROM folders WHERE run_id = ?", (run_id,))

    aggregates = conn.execute(
        """
        SELECT parent_path AS path,
               COUNT(*) AS file_count,
               SUM(size) AS total_size,
               MIN(mtime) AS mtime_min,
               MAX(mtime) AS mtime_max
        FROM files
        WHERE run_id = ?
        GROUP BY parent_path
        """,
        (run_id,),
    ).fetchall()

    dominant: dict[str, str] = {}
    for r in conn.execute(
        """
        SELECT parent_path, mime_category, SUM(size) AS s
        FROM files
        WHERE run_id = ? AND mime_category IS NOT NULL
        GROUP BY parent_path, mime_category
        ORDER BY parent_path, s DESC
        """,
        (run_id,),
    ):
        dominant.setdefault(r["parent_path"], r["mime_category"])

    rows: list[tuple] = []
    for r in aggregates:
        path = r["path"]
        parent = _folder_parent(path, root)
        depth = _folder_depth(path, root)
        rows.append((
            run_id, path, parent, depth,
            r["file_count"], r["total_size"] or 0,
            0,  # max_depth_below — filled below
            r["mtime_min"], r["mtime_max"],
            dominant.get(path),
        ))

    folder_paths = {row[1] for row in rows}
    max_depth_below: dict[str, int] = {p: 0 for p in folder_paths}
    for path in folder_paths:
        cur = path
        while True:
            parent = _folder_parent(cur, root)
            if parent is None or parent not in max_depth_below:
                break
            d = _folder_depth(path, root) - _folder_depth(parent, root)
            if d > max_depth_below[parent]:
                max_depth_below[parent] = d
            cur = parent

    final_rows = [
        (run_id, path, parent, depth, fc, ts,
         max_depth_below.get(path, 0), mn, mx, dom)
        for (run_id, path, parent, depth, fc, ts, _, mn, mx, dom) in rows
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


def staleness_buckets(conn: sqlite3.Connection, run_id: int,
                       now: Optional[datetime] = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    cutoffs = {
        "<1y":  now - timedelta(days=365),
        "1-3y": now - timedelta(days=365 * 3),
        "3-5y": now - timedelta(days=365 * 5),
    }
    buckets: list[dict] = []
    rows = conn.execute(
        """
        SELECT mtime, size FROM files
        WHERE run_id = ? AND mtime IS NOT NULL
        """,
        (run_id,),
    ).fetchall()

    counters = {"<1y": [0, 0], "1-3y": [0, 0], "3-5y": [0, 0], "5y+": [0, 0]}
    for r in rows:
        try:
            mt = datetime.fromisoformat(r["mtime"])
        except (TypeError, ValueError):
            continue
        if mt.tzinfo is None:
            mt = mt.replace(tzinfo=timezone.utc)
        if mt >= cutoffs["<1y"]:
            key = "<1y"
        elif mt >= cutoffs["1-3y"]:
            key = "1-3y"
        elif mt >= cutoffs["3-5y"]:
            key = "3-5y"
        else:
            key = "5y+"
        counters[key][0] += 1
        counters[key][1] += r["size"] or 0

    for k in ("<1y", "1-3y", "3-5y", "5y+"):
        c, s = counters[k]
        buckets.append({"bucket": k, "file_count": c, "total_size": s})
    return buckets


def top_duplicates(conn: sqlite3.Connection, run_id: int,
                   limit: int = 100) -> list[dict]:
    rows = conn.execute(
        """
        SELECT sha256, file_count, file_size, wasted_bytes
        FROM duplicates
        WHERE run_id = ?
        ORDER BY wasted_bytes DESC
        LIMIT ?
        """,
        (run_id, limit),
    ).fetchall()
    out = []
    for r in rows:
        sample = conn.execute(
            "SELECT path FROM files WHERE run_id = ? AND sha256 = ? LIMIT 5",
            (run_id, r["sha256"]),
        ).fetchall()
        out.append({**dict(r), "sample_paths": [s["path"] for s in sample]})
    return out


def category_distribution(conn: sqlite3.Connection, run_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT COALESCE(mime_category, 'other') AS category,
               COUNT(*) AS file_count,
               SUM(size) AS total_size
        FROM files
        WHERE run_id = ?
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
        WHERE run_id = ?
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
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=max_age_days)).isoformat()
    placeholders = ",".join("?" for _ in categories)
    sql = f"""
        SELECT id, path, parent_path, name, extension, size, mtime, sha256,
               mime_type, mime_category
        FROM files
        WHERE run_id = ?
          AND mime_category IN ({placeholders})
          AND size BETWEEN ? AND ?
          AND (mtime IS NULL OR mtime >= ?)
        ORDER BY mtime DESC
    """
    params = [run_id, *categories, min_size, max_size, cutoff]
    seen_sha: set[str] = set()
    for row in conn.execute(sql, params):
        sha = row["sha256"]
        if include_one_per_dup and sha:
            if sha in seen_sha:
                continue
            seen_sha.add(sha)
        yield dict(row)
