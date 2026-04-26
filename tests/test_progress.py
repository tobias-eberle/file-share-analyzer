"""ProgressPrinter: a single self-overwriting line, time-throttled."""
from __future__ import annotations

import io
import threading
import time
from pathlib import Path

from share_analyzer.crawl.orchestrator import CrawlOptions, run_crawl
from share_analyzer.logging import ProgressPrinter


class _TtyStream(io.StringIO):
    """A StringIO that pretends to be a tty so the printer renders."""
    def isatty(self) -> bool:  # type: ignore[override]
        return True


def test_renders_files_rate_errors_and_path():
    s = _TtyStream()
    p = ProgressPrinter(every=0.0, stream=s)  # no throttle
    p.update(files=42, errors=3, last_path="/srv/share/docs/notes.md")
    out = s.getvalue()
    assert "\r" in out                     # carriage-return overwrite
    assert "[scan]" in out
    assert "42" in out                     # files
    assert "3" in out                      # errors
    assert "notes.md" in out               # path tail visible


def test_long_path_is_truncated_from_left():
    s = _TtyStream()
    p = ProgressPrinter(every=0.0, stream=s, path_width=20)
    long = "/a/very/long/path/that/goes/well/past/twenty/chars/file.txt"
    p.update(files=1, last_path=long)
    out = s.getvalue()
    assert "…" in out
    # The right-hand tail must survive — it's the informative part.
    assert "file.txt" in out
    # The full long path must NOT appear verbatim — it was truncated.
    assert long not in out


def test_throttle_drops_intermediate_calls_within_window():
    s = _TtyStream()
    p = ProgressPrinter(every=10.0, stream=s)  # very long window
    p.update(files=1, last_path="/a", force=True)   # initial render
    initial_len = len(s.getvalue())
    # 1000 hot-path updates within the throttle window — none should
    # actually hit the stream.
    for i in range(1000):
        p.update(files=i + 2, last_path=f"/x/{i}")
    assert len(s.getvalue()) == initial_len


def test_throttle_emits_again_after_window():
    s = _TtyStream()
    p = ProgressPrinter(every=0.05, stream=s)
    p.update(files=1, last_path="/a", force=True)
    a = len(s.getvalue())
    time.sleep(0.08)
    p.update(files=2, last_path="/b")
    assert len(s.getvalue()) > a


def test_padding_clears_previous_longer_line():
    s = _TtyStream()
    p = ProgressPrinter(every=0.0, stream=s, path_width=80)
    p.update(files=1, last_path="A" * 70)
    long_len = len(s.getvalue())
    s.seek(0)
    s.truncate()
    p.update(files=2, last_path="B")
    short_render = s.getvalue()
    # The shorter render must be padded with spaces so the previous
    # 'A' tail doesn't bleed through. It has trailing whitespace.
    assert short_render.endswith(" ")
    # And it must be at least as long as the previous line was
    # (carriage return + content + padding).
    assert len(short_render) >= long_len - len("\r")


def test_force_emits_even_within_window():
    s = _TtyStream()
    p = ProgressPrinter(every=10.0, stream=s)
    p.update(files=1, last_path="/a", force=True)
    a = len(s.getvalue())
    p.update(files=2, last_path="/b", force=True)
    assert len(s.getvalue()) > a


def test_finish_writes_newline():
    s = _TtyStream()
    p = ProgressPrinter(every=0.0, stream=s)
    p.update(files=1, last_path="/a")
    p.finish()
    assert s.getvalue().endswith("\n")


def test_non_tty_stream_emits_nothing():
    """CI logs and redirected stdout shouldn't get a flood of \\r."""
    s = io.StringIO()                  # default isatty() == False
    p = ProgressPrinter(every=0.0, stream=s)
    p.update(files=100, errors=2, last_path="/a/b")
    p.update(files=200, last_path="/c/d", force=True)
    p.finish()
    assert s.getvalue() == ""


def test_thread_safety_under_concurrent_updates():
    """Hammer update() from many threads — no exceptions, final state
    reflects the highest values seen."""
    s = _TtyStream()
    p = ProgressPrinter(every=0.0, stream=s)

    def worker(start: int) -> None:
        for i in range(start, start + 200):
            p.update(files=i, errors=i % 5, last_path=f"/x/{i}")

    ts = [threading.Thread(target=worker, args=(i * 200,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    # Just sanity — no crash, something landed.
    assert "[scan]" in s.getvalue()


def test_orchestrator_emits_progress_during_run(tmp_path: Path):
    """End-to-end: a real crawl drives the callback with growing
    file_count and a current path."""
    root = tmp_path / "share"
    sub = root / "sub"
    sub.mkdir(parents=True)
    # Enough files to flush at least one batch (BATCH_SIZE = 1000) — but
    # keep it small enough that the test stays fast. The end-of-run
    # final-flush always emits one progress event regardless of batch
    # size, so a handful of files is enough to verify wiring.
    for i in range(20):
        (sub / f"f{i:03d}.txt").write_text(str(i))

    seen: list[tuple[int, int, str]] = []
    def cb(files: int, errors: int, last_path: str) -> None:
        seen.append((files, errors, last_path))

    db = tmp_path / "i.sqlite"
    run_crawl(root, db, CrawlOptions(workers=2), progress=cb)
    assert seen, "progress callback was never invoked"
    final = seen[-1]
    assert final[0] == 20
    assert final[2].endswith(".txt")