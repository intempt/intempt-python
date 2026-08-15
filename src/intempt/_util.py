"""Small helpers.

Portions derived from mixpanel-python (Apache License 2.0), as recorded in
NOTICE. ``ensure_timestamp`` and ``chunk`` follow its timestamp normalisation and
batching helper; both were changed to reject bad input rather than coerce it.

Copyright 2026 Intempt Technologies
Licensed under the Apache License, Version 2.0.
"""

from __future__ import annotations

import datetime as _dt
import math
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any, TypeVar

from ._errors import IntemptConfigError

T = TypeVar("T")

_LOG_LEVELS = ("debug", "info", "warning", "error")


def ensure_timestamp(value: _dt.datetime | int | float) -> int:
    """Epoch milliseconds from a datetime or a number.

    A naive datetime is treated as UTC rather than local time. Local time would
    make the same code produce different events on a developer laptop and a
    server, which is the kind of difference nobody notices until a report is
    wrong.
    """
    if isinstance(value, _dt.datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=_dt.timezone.utc)
        return int(moment.timestamp() * 1000)

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise IntemptConfigError(
            f"timestamp must be a datetime or epoch milliseconds, got {type(value).__name__}"
        )
    if not math.isfinite(value):
        raise IntemptConfigError("timestamp must be a finite number of milliseconds")
    return int(value)


def chunk(items: Sequence[T], size: int) -> list[list[T]]:
    """Split into equal-sized chunks, last chunk being the remainder.

    A size below one is rejected: the loop would never advance, so clamping it
    silently would hang instead of failing.
    """
    if size < 1:
        raise IntemptConfigError("chunk size must be at least 1")
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def compact(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is ``None`` so they never reach the wire.

    Only ``None`` is dropped. ``False``, ``0`` and ``""`` are values the caller
    chose and are kept.
    """
    return {k: v for k, v in mapping.items() if v is not None}


def assert_logger(logger: Any) -> None:
    """Reject a logger that would blow up the first time something goes wrong.

    Checked at construction rather than at the first log call, because the first
    log call is usually inside an error path and a failure there hides the
    original error.
    """
    missing = [name for name in _LOG_LEVELS if not callable(getattr(logger, name, None))]
    if missing:
        raise IntemptConfigError(
            "logger must implement " + ", ".join(_LOG_LEVELS) + "; missing: " + ", ".join(missing)
        )


def non_blank(value: Any, method: str, field: str) -> str:
    """Return a non-blank string or raise, naming the method and the field.

    Truthiness is not enough: a run of spaces is truthy and is not an identifier.
    """
    if not isinstance(value, str) or not value.strip():
        raise IntemptConfigError(f"{method}: {field} must be a non-empty string")
    return value


def require_identifier(options: Mapping[str, Any], method: str) -> None:
    """At least one of user_id or account_id must be present and non-blank."""
    for field in ("user_id", "account_id", "profile_id"):
        value = options.get(field)
        if isinstance(value, str) and value.strip():
            return
    raise IntemptConfigError(f"{method}: one of user_id or account_id is required")


def iter_pairs(items: Iterable[T]) -> Iterator[tuple[int, T]]:
    """enumerate(), named so call sites read as intent rather than mechanics."""
    return enumerate(items)
