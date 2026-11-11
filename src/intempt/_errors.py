"""Errors raised by the SDK.

Copyright 2026 Intempt Technologies
Licensed under the Apache License, Version 2.0.
"""

from __future__ import annotations

from typing import Any


class IntemptError(Exception):
    """Base class, so callers can catch everything this SDK raises."""


class IntemptConfigError(IntemptError, ValueError):
    """Bad configuration or bad arguments. Never retried."""


class IntemptApiError(IntemptError):
    """The API answered, or the transport failed.

    ``status`` is ``None`` for a transport failure or a timeout. ``reached`` is
    False only when the request never left (DNS, refused, connect timeout): that
    batch is safe to send again. Once the request was written the server may hold
    it, so a timeout or a broken connection after that point is not resent
    (INT-7436).
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: str | None = None,
        retry_after_ms: int | None = None,
        cause: BaseException | None = None,
        reached: bool = True,
        timed_out: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.retry_after_ms = retry_after_ms
        self.cause = cause
        self.reached = reached
        self.timed_out = timed_out

    @property
    def retryable(self) -> bool:
        """True for 429 and 503, the only statuses the SDK resends (once; INT-7436)."""
        return self.status in (429, 503)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"IntemptApiError(status={self.status!r}, message={str(self)!r})"


def _describe(value: Any) -> str:
    """Type name for an error message, without echoing the value.

    The value may be a credential or a customer's payload, and error messages end
    up in logs.
    """
    return type(value).__name__
