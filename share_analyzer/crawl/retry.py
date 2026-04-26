"""Transient-error retry helper for SMB-mounted crawls.

A small wrapper around the open/scandir call sites. Intentionally
self-contained — no global state, no thread-locals — so the orchestrator
keeps deciding when threads run.
"""
from __future__ import annotations

import errno
import time
from typing import Any, Callable, Optional, Sequence


# Errno values that usually clear up on a brief retry.
TRANSIENT_ERRNOS: frozenset[int] = frozenset(
    code for code in (
        getattr(errno, "ETIMEDOUT", None),
        getattr(errno, "ECONNRESET", None),
        getattr(errno, "EHOSTUNREACH", None),
        getattr(errno, "ENETUNREACH", None),
        getattr(errno, "ENETDOWN", None),
        getattr(errno, "ENETRESET", None),
        getattr(errno, "EAGAIN", None),
        getattr(errno, "EBUSY", None),
        getattr(errno, "EPIPE", None),
        getattr(errno, "EIO", None),
    )
    if code is not None
)

# Windows-only error codes (OSError.winerror) for the SMB net-name family.
TRANSIENT_WINERRORS: frozenset[int] = frozenset({
    53,    # ERROR_BAD_NETPATH
    58,    # ERROR_BAD_NET_RESP
    59,    # ERROR_UNEXP_NET_ERR
    64,    # ERROR_NETNAME_DELETED
    67,    # ERROR_BAD_NET_NAME
    121,   # ERROR_SEM_TIMEOUT
    1231,  # ERROR_NETWORK_UNREACHABLE
    1232,  # ERROR_HOST_UNREACHABLE
})

DEFAULT_BACKOFF: tuple[float, ...] = (0.2, 1.0, 5.0)


def is_transient(exc: BaseException) -> bool:
    """True if the OS told us 'try again' rather than 'no'."""
    if not isinstance(exc, OSError):
        return False
    if exc.errno in TRANSIENT_ERRNOS:
        return True
    winerror = getattr(exc, "winerror", None)
    if winerror is not None and winerror in TRANSIENT_WINERRORS:
        return True
    return False


def with_retry(
    fn: Callable[[], Any],
    *,
    backoff: Sequence[float] = DEFAULT_BACKOFF,
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
) -> Any:
    """Call `fn`; on a transient OSError sleep for `backoff[i]` and retry.

    `len(backoff)` defines the number of retries (so total attempts =
    len(backoff) + 1). Permanent errors propagate immediately. The
    `on_retry` callback fires once per retry with
    `(attempt_number, exception, sleep_seconds)` so callers can log.
    """
    attempts_after_first = len(backoff)
    last_exc: Optional[BaseException] = None
    for attempt in range(attempts_after_first + 1):
        try:
            return fn()
        except OSError as e:
            if not is_transient(e) or attempt == attempts_after_first:
                raise
            sleep_s = backoff[attempt]
            if on_retry is not None:
                try:
                    on_retry(attempt + 1, e, sleep_s)
                except Exception:  # pragma: no cover — defensive
                    pass
            time.sleep(sleep_s)
            last_exc = e
    # Unreachable — the loop either returns or raises.
    raise last_exc  # pragma: no cover
