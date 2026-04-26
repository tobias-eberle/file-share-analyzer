"""HealthMonitor: error classification + sliding window + root probe."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from share_analyzer.crawl.health import HealthMonitor, _looks_like_network_error


def test_network_class_classification():
    # Realistic reason strings that surface from OSError formatting.
    assert _looks_like_network_error("OSError: [Errno 110] ETIMEDOUT")
    assert _looks_like_network_error("ConnectionResetError: [Errno 104]")
    assert _looks_like_network_error("OSError: [WinError 64] ERROR_NETNAME_DELETED")
    assert _looks_like_network_error("OSError: [WinError 53] The network path was not found")


def test_non_network_class_ignored():
    assert not _looks_like_network_error("PermissionError: [Errno 13] Permission denied")
    assert not _looks_like_network_error("FileNotFoundError: [Errno 2] No such file")
    assert not _looks_like_network_error("symlink-skipped")


def test_record_error_filters_noise(tmp_path: Path):
    h = HealthMonitor(str(tmp_path))
    h.record_error("PermissionError: [Errno 13] Permission denied")
    h.record_error("symlink-skipped")
    assert h.recent_error_count() == 0

    h.record_error("OSError: [Errno 110] ETIMEDOUT")
    assert h.recent_error_count() == 1


def test_window_drops_old_errors(tmp_path: Path):
    h = HealthMonitor(str(tmp_path), window_s=0.05)
    h.record_error("ETIMEDOUT")
    assert h.recent_error_count() == 1
    time.sleep(0.08)
    assert h.recent_error_count() == 0


def test_disconnected_below_threshold_returns_false(tmp_path: Path):
    h = HealthMonitor(str(tmp_path), error_threshold=10)
    for _ in range(5):
        h.record_error("ETIMEDOUT")
    assert not h.is_disconnected()


def test_disconnected_with_reachable_root_returns_false(tmp_path: Path):
    """Threshold hit but root is fine — local errors, share OK."""
    h = HealthMonitor(str(tmp_path), error_threshold=3)
    for _ in range(5):
        h.record_error("ETIMEDOUT")
    assert not h.is_disconnected()


def test_disconnected_with_missing_root_returns_true(tmp_path: Path):
    """Threshold hit AND root unreachable — escalate to disconnect."""
    bogus = tmp_path / "does-not-exist"
    h = HealthMonitor(str(bogus), error_threshold=3, recheck_interval_s=0.0)
    for _ in range(5):
        h.record_error("ETIMEDOUT")
    assert h.is_disconnected()


def test_advisory_only_when_elevated(tmp_path: Path):
    h = HealthMonitor(str(tmp_path), error_threshold=40)
    assert h.advisory() is None
    for _ in range(15):
        h.record_error("ETIMEDOUT")
    msg = h.advisory()
    assert msg is not None
    assert "high error rate" in msg


def test_orchestrator_marks_run_disconnected(tmp_path: Path):
    """Wire the monitor through run_crawl: a flood of network-class errors
    on a non-existent root must end with status='disconnected', not
    'completed'."""
    from share_analyzer.crawl.orchestrator import CrawlOptions, run_crawl
    from share_analyzer.crawl.walker import WalkError, Walker

    bogus = str(tmp_path / "vanished")

    class _DisconnectedWalker:
        """Synthesises a flood of network-class WalkErrors so the
        HealthMonitor crosses its threshold."""
        def walk(self):
            for i in range(60):
                yield WalkError(
                    path=f"{bogus}/missing-{i}.txt",
                    reason="OSError: [WinError 64] ERROR_NETNAME_DELETED",
                )

    db = tmp_path / "i.sqlite"
    result = run_crawl(
        bogus, db,
        CrawlOptions(
            workers=1, dir_workers=1,
            health_error_threshold=10, health_window_s=10.0,
        ),
        walker=_DisconnectedWalker(),
    )
    assert result.status == "disconnected"
    assert result.advisory and "unreachable" in result.advisory
