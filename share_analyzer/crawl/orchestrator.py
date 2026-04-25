"""Drives the crawl: walker → worker pool → sink, with checkpointing.

Threading model:
- Main thread runs the walker. Files go onto `in_q` for fingerprinting;
  walker errors and unchanged-on-rescan files go directly onto `out_q`
  so they reach the sink without racing with the writer thread.
- Worker threads consume `in_q`, fingerprint, push (entry, fp, state)
  onto `out_q`. On a sentinel they forward one to `out_q` and exit.
- A single writer thread drains `out_q` and is the only thread that
  ever calls into the sink during the crawl. If it raises, we capture
  the exception, set the abort event so workers and the walker bail
  out of `put`, and re-raise on the main thread after join.

Rescan: when `previous_run_id` is set, the main thread classifies each
walker entry against the prior snapshot before queuing. Unchanged files
skip the fingerprint workers entirely; new and modified files take the
full path. Files in the prior run not seen by the walker are inserted
as `state='deleted'` rows in a single SQL statement after the walk.
"""
from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from share_analyzer.crawl.fingerprint import Fingerprint, Fingerprinter, StreamingFingerprinter
from share_analyzer.crawl.rescan import RescanContext
from share_analyzer.crawl.sink import Sink, SqliteSink
from share_analyzer.crawl.walker import FileEntry, LocalScandirWalker, WalkError, Walker
from share_analyzer.index.queries import materialize_folders

log = logging.getLogger("share_analyzer.crawl")


@dataclass
class CrawlResult:
    run_id: int
    file_count: int
    error_count: int
    status: str
    previous_run_id: Optional[int] = None
    state_counts: Optional[dict[str, int]] = None


@dataclass
class CrawlOptions:
    workers: int = 8
    hash_cap_bytes: int = 100 * 1024 * 1024
    queue_size: int = 1024
    checkpoint_every: int = 10_000
    exclude_globs: Sequence[str] = ()
    follow_symlinks: bool = False
    previous_run_id: Optional[int] = None


_SENTINEL: object = object()
_PUT_TIMEOUT = 0.25  # seconds — short so abort is observed promptly


def _put(q: queue.Queue, item: object, abort: threading.Event) -> bool:
    """Block on `q.put` while remaining responsive to abort."""
    while not abort.is_set():
        try:
            q.put(item, timeout=_PUT_TIMEOUT)
            return True
        except queue.Full:
            continue
    return False


def run_crawl(
    root: str | Path,
    db_path: Path,
    options: Optional[CrawlOptions] = None,
    *,
    walker: Optional[Walker] = None,
    fingerprinter: Optional[Fingerprinter] = None,
    sink: Optional[Sink] = None,
    progress: Optional[Callable[[int, int, int], None]] = None,
) -> CrawlResult:
    options = options or CrawlOptions()
    walker = walker or LocalScandirWalker(
        root,
        exclude_globs=options.exclude_globs,
        follow_symlinks=options.follow_symlinks,
    )
    fingerprinter = fingerprinter or StreamingFingerprinter(options.hash_cap_bytes)
    sink = sink or SqliteSink(db_path)

    rescan_ctx: Optional[RescanContext] = None
    if options.previous_run_id is not None:
        rescan_ctx = RescanContext(sink.conn, options.previous_run_id)  # type: ignore[attr-defined]
        log.info("rescan-context", extra={
            "previous_run_id": options.previous_run_id,
            "prior_files": len(rescan_ctx),
        })

    run_id = sink.begin_run(
        str(root),
        workers=options.workers,
        hash_cap_bytes=options.hash_cap_bytes,
        previous_run_id=options.previous_run_id,
    )
    log.info("crawl-start", extra={"run_id": run_id, "root": str(root)})

    default_state = "baseline" if rescan_ctx is None else "added"
    in_q: queue.Queue = queue.Queue(maxsize=options.queue_size)
    out_q: queue.Queue = queue.Queue(maxsize=options.queue_size)

    file_count = 0
    error_count = 0
    abort = threading.Event()
    writer_exc: list[BaseException] = []
    last_wal_truncate_at = 0  # mutated by the writer thread only

    def worker() -> None:
        while True:
            item = in_q.get()
            try:
                if item is _SENTINEL:
                    if not abort.is_set():
                        _put(out_q, _SENTINEL, abort)
                    return
                if abort.is_set():
                    continue
                entry, state = item
                try:
                    fp = fingerprinter.fingerprint(entry)
                except Exception as e:  # pragma: no cover — defensive
                    fp = Fingerprint(
                        sha256=None, mime_type=None, mime_category="other",
                        owner=None, error=f"{type(e).__name__}: {e}",
                    )
                _put(out_q, (entry, fp, state), abort)
            finally:
                in_q.task_done()

    threads = [
        threading.Thread(target=worker, name=f"fp-{i}", daemon=True)
        for i in range(max(1, options.workers))
    ]
    for t in threads:
        t.start()

    def writer() -> None:
        nonlocal file_count, error_count, last_wal_truncate_at
        sentinels = 0
        batch: list[tuple[FileEntry, Fingerprint, str]] = []
        last_path: Optional[str] = None
        try:
            while sentinels < len(threads):
                item = out_q.get()
                try:
                    if item is _SENTINEL:
                        sentinels += 1
                        continue
                    if isinstance(item, WalkError):
                        sink.write_errors([item])
                        error_count += 1
                        continue
                    entry, fp, state = item
                    batch.append((entry, fp, state))
                    last_path = entry.path
                    if fp.error:
                        error_count += 1
                    if len(batch) >= sink.BATCH_SIZE:
                        file_count += sink.write_files(batch)
                        batch.clear()
                        if file_count - last_wal_truncate_at >= options.checkpoint_every:
                            sink.checkpoint(last_path, file_count)
                            sink.wal_checkpoint()
                            last_wal_truncate_at = file_count
                            if progress:
                                progress(file_count, error_count, 0)
                finally:
                    out_q.task_done()
            if batch:
                file_count += sink.write_files(batch)
            sink.checkpoint(last_path, file_count)
        except BaseException as e:  # noqa: BLE001 — capture for re-raise
            writer_exc.append(e)
            abort.set()
            # Drain remaining items so workers don't block on out_q.put.
            while True:
                try:
                    out_q.get_nowait()
                    out_q.task_done()
                except queue.Empty:
                    break

    writer_thread = threading.Thread(target=writer, name="sink-writer", daemon=True)
    writer_thread.start()

    try:
        for item in walker.walk():
            if abort.is_set():
                break
            if isinstance(item, WalkError):
                if not _put(out_q, item, abort):
                    break
                continue

            if rescan_ctx is None:
                if not _put(in_q, (item, default_state), abort):
                    break
                continue

            state, prior_fp = rescan_ctx.classify(item)
            if state == "unchanged" and prior_fp is not None:
                # Skip workers entirely — no rehash, no MIME re-detect.
                if not _put(out_q, (item, prior_fp, "unchanged"), abort):
                    break
            else:
                if not _put(in_q, (item, state), abort):
                    break
    finally:
        for _ in threads:
            in_q.put(_SENTINEL)
        for t in threads:
            t.join()
        writer_thread.join()

    if writer_exc:
        sink.end_run(
            file_count=file_count, error_count=error_count, status="failed",
        )
        sink.close()
        log.error("crawl-failed", extra={"run_id": run_id})
        raise writer_exc[0]

    deleted_count = 0
    if rescan_ctx is not None:
        deleted_count = sink.copy_deleted_from_previous(  # type: ignore[attr-defined]
            options.previous_run_id  # type: ignore[arg-type]
        )
        log.info("rescan-deleted", extra={"run_id": run_id, "n": deleted_count})

    log.info("materializing-folders", extra={"run_id": run_id})
    materialize_folders(sink.conn, run_id)  # type: ignore[attr-defined]

    state_counts = None
    if rescan_ctx is not None:
        from share_analyzer.index.queries import changed_files_summary
        state_counts = changed_files_summary(sink.conn, run_id)  # type: ignore[attr-defined]

    sink.end_run(file_count=file_count, error_count=error_count, status="completed")
    sink.wal_checkpoint()
    sink.close()
    log.info("crawl-end", extra={
        "run_id": run_id, "files": file_count, "errors": error_count,
    })
    return CrawlResult(
        run_id=run_id,
        file_count=file_count,
        error_count=error_count,
        status="completed",
        previous_run_id=options.previous_run_id,
        state_counts=state_counts,
    )
