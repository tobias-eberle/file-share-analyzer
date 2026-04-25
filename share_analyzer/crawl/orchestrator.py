"""Drives the crawl: walker → worker pool → sink, with checkpointing.

Main thread runs the walker and dispatches FileEntry objects to a thread
pool of fingerprint workers. Completed (entry, fingerprint) pairs go onto
a single-writer queue consumed by the sink. This keeps memory bounded
and SQLite writes serialized.
"""
from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from share_analyzer.crawl.fingerprint import Fingerprint, Fingerprinter, StreamingFingerprinter
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


@dataclass
class CrawlOptions:
    workers: int = 8
    hash_cap_bytes: int = 100 * 1024 * 1024
    queue_size: int = 1024
    checkpoint_every: int = 10_000
    exclude_globs: Sequence[str] = ()
    follow_symlinks: bool = False


_SENTINEL: object = object()


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

    run_id = sink.begin_run(
        str(root),
        workers=options.workers,
        hash_cap_bytes=options.hash_cap_bytes,
    )
    log.info("crawl-start", extra={"run_id": run_id, "root": str(root)})

    in_q: queue.Queue = queue.Queue(maxsize=options.queue_size)
    out_q: queue.Queue = queue.Queue(maxsize=options.queue_size)

    file_count = 0
    error_count = 0

    def worker() -> None:
        while True:
            item = in_q.get()
            if item is _SENTINEL:
                in_q.task_done()
                out_q.put(_SENTINEL)
                return
            try:
                fp = fingerprinter.fingerprint(item)
            except Exception as e:  # pragma: no cover — defensive
                fp = Fingerprint(
                    sha256=None, mime_type=None, mime_category="other",
                    owner=None, error=f"{type(e).__name__}: {e}",
                )
            out_q.put((item, fp))
            in_q.task_done()

    threads = [
        threading.Thread(target=worker, name=f"fp-{i}", daemon=True)
        for i in range(max(1, options.workers))
    ]
    for t in threads:
        t.start()

    def writer() -> None:
        nonlocal file_count, error_count
        sentinels = 0
        batch: list[tuple[FileEntry, Fingerprint]] = []
        last_path: Optional[str] = None
        while sentinels < len(threads):
            item = out_q.get()
            try:
                if item is _SENTINEL:
                    sentinels += 1
                    continue
                entry, fp = item
                batch.append((entry, fp))
                last_path = entry.path
                if fp.error:
                    error_count += 1
                if len(batch) >= sink.BATCH_SIZE:
                    file_count += sink.write_files(batch)
                    batch.clear()
                    if file_count % options.checkpoint_every < sink.BATCH_SIZE:
                        sink.checkpoint(last_path, file_count)
                        if progress:
                            progress(file_count, error_count, 0)
            finally:
                out_q.task_done()
        if batch:
            file_count += sink.write_files(batch)
        sink.checkpoint(last_path, file_count)

    writer_thread = threading.Thread(target=writer, name="sink-writer", daemon=True)
    writer_thread.start()

    walk_errors = 0
    try:
        for item in walker.walk():
            if isinstance(item, WalkError):
                sink.write_errors([item])
                walk_errors += 1
                error_count += 1
                continue
            in_q.put(item)
    finally:
        for _ in threads:
            in_q.put(_SENTINEL)
        for t in threads:
            t.join()
        writer_thread.join()

    log.info("materializing-folders", extra={"run_id": run_id})
    materialize_folders(sink.conn, run_id)  # type: ignore[attr-defined]

    sink.end_run(file_count=file_count, error_count=error_count, status="completed")
    sink.close()
    log.info("crawl-end", extra={
        "run_id": run_id, "files": file_count, "errors": error_count,
    })
    return CrawlResult(
        run_id=run_id,
        file_count=file_count,
        error_count=error_count,
        status="completed",
    )
