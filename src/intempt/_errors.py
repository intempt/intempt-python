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

    ``status`` is ``None`` for a transport failure or a timeout, which is why
    ``retryable`` treats that case as retryable: nothing came back to say the
    request was rejected on its merits.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: str | None = None,
        retry_after_ms: int | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.retry_after_ms = retry_after_ms
        self.cause = cause

    @property
    def retryable(self) -> bool:
        """True for statuses worth retrying: 408, 429 and any 5xx."""
        if self.status is None:
            return True
        return self.status in (408, 429) or self.status >= 500

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"IntemptApiError(status={self.status!r}, message={str(self)!r})"


def _describe(value: Any) -> str:
    """Type name for an error message, without echoing the value.

    The value may be a credential or a customer's payload, and error messages end
    up in logs.
    """
    return type(value).__name__
