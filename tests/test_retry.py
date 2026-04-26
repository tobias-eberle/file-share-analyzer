"""Transient-error retry helper."""
from __future__ import annotations

import errno
import time

import pytest

from share_analyzer.crawl.retry import (
    TRANSIENT_ERRNOS, TRANSIENT_WINERRORS, is_transient, with_retry,
)


def _oserr(code: int, msg: str = "boom") -> OSError:
    return OSError(code, msg)


def test_is_transient_classifies_errno():
    assert is_transient(_oserr(errno.ETIMEDOUT))
    assert is_transient(_oserr(errno.ECONNRESET))
    assert is_transient(_oserr(errno.EAGAIN))


def test_is_transient_rejects_permanent_errors():
    assert not is_transient(_oserr(errno.ENOENT))
    assert not is_transient(_oserr(errno.EACCES))
    assert not is_transient(_oserr(errno.EISDIR))


def test_is_transient_rejects_non_oserror():
    assert not is_transient(ValueError("nope"))
    assert not is_transient(RuntimeError("also nope"))


def test_is_transient_classifies_winerror():
    e = OSError("net path bad")
    e.winerror = 53  # ERROR_BAD_NETPATH
    assert is_transient(e)


def test_with_retry_succeeds_on_first_try():
    calls = []
    def fn():
        calls.append(1)
        return "ok"
    assert with_retry(fn) == "ok"
    assert len(calls) == 1


def test_with_retry_succeeds_after_transient_failures():
    calls = [0]
    def fn():
        calls[0] += 1
        if calls[0] < 3:
            raise _oserr(errno.ETIMEDOUT)
        return "ok"
    # Use zero backoff so the test is fast.
    assert with_retry(fn, backoff=(0, 0, 0)) == "ok"
    assert calls[0] == 3


def test_with_retry_gives_up_after_attempts_exhausted():
    calls = [0]
    def fn():
        calls[0] += 1
        raise _oserr(errno.ETIMEDOUT)
    with pytest.raises(OSError):
        with_retry(fn, backoff=(0, 0))
    # 1 initial + 2 retries = 3 attempts.
    assert calls[0] == 3


def test_with_retry_does_not_retry_permanent_errors():
    calls = [0]
    def fn():
        calls[0] += 1
        raise _oserr(errno.EACCES)
    with pytest.raises(OSError):
        with_retry(fn, backoff=(0, 0, 0))
    assert calls[0] == 1


def test_with_retry_invokes_on_retry_callback():
    calls = [0]
    notes: list[tuple[int, int, float]] = []
    def fn():
        calls[0] += 1
        if calls[0] < 3:
            raise _oserr(errno.ECONNRESET)
        return "done"
    def on_retry(attempt, exc, sleep_s):
        notes.append((attempt, exc.errno, sleep_s))
    with_retry(fn, backoff=(0, 0), on_retry=on_retry)
    assert notes == [(1, errno.ECONNRESET, 0), (2, errno.ECONNRESET, 0)]


def test_with_retry_callback_failure_does_not_break_retry():
    """A logging hook crashing must not abort the retry chain."""
    calls = [0]
    def fn():
        calls[0] += 1
        if calls[0] < 2:
            raise _oserr(errno.ETIMEDOUT)
        return "ok"
    def bad_callback(*_):
        raise RuntimeError("logger blew up")
    assert with_retry(fn, backoff=(0,), on_retry=bad_callback) == "ok"


def test_with_retry_with_zero_attempts_does_not_retry():
    calls = [0]
    def fn():
        calls[0] += 1
        raise _oserr(errno.ETIMEDOUT)
    with pytest.raises(OSError):
        with_retry(fn, backoff=())
    assert calls[0] == 1


def test_with_retry_actually_sleeps_between_attempts():
    """The point of backoff is real wall-clock delay; sanity-check it."""
    calls = [0]
    def fn():
        calls[0] += 1
        if calls[0] < 2:
            raise _oserr(errno.ETIMEDOUT)
        return "ok"
    t0 = time.monotonic()
    with_retry(fn, backoff=(0.05,))
    assert time.monotonic() - t0 >= 0.05
