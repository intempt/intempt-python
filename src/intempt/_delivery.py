"""The send policy and how a failed send is reported (INT-7436, INT-7444).

One table for batch mode, direct mode and consent, so the three cannot drift:

* answered 2xx            sent
* answered 503 or 429     one resend after Retry-After clamped to [1 s, 60 s] (1 s when
                          absent); a wait past 5 s drops instead of sleeping, because in a server
                          process the wait runs on a caller's thread
* any other answer        dropped (413 included: halving and resending is a retry)
* timeout after the write dropped: the server may already hold the events, so a resend risks
                          duplicates
* never reached           kept by the buffer for its next flush; direct mode has no queue and drops
* stale pooled socket     not an outcome here: the transport reconnects once before it reports (R5)

Nothing here raises to the caller. The outcome goes to the ``on_delivery_failure`` listener once
per batch. Rulings: brain plan2 CONTRACT-R2 S1/S2 and DECISIONS 7436/7444.

Copyright 2026 Intempt Technologies
Licensed under the Apache License, Version 2.0.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Callable, Literal

from ._errors import IntemptApiError

RESEND_STATUSES = (429, 503)
RESEND_WAIT_MIN_S = 1.0
RESEND_WAIT_MAX_S = 60.0
#: Server SDKs only. Past it the batch is dropped and reported rather than slept on.
SERVER_WAIT_CAP_S = 5.0

DeliveryKind = Literal["transport", "terminal", "dropped"]


@dataclass(frozen=True)
class DeliveryFailure:
    """The final outcome of one batch that was not delivered.

    ``transport``: never reached the server and still queued (``terminal`` False). It is reported
    once per event; if those events are dropped later, a ``dropped`` or ``terminal`` report follows.
    ``terminal``: the server answered a drop status, or the one resend failed.
    ``dropped``: given up client-side (timeout after the write, queue overflow, the 5 s cap, close).
    ``retry_after`` is in seconds.
    """

    kind: DeliveryKind
    event_count: int
    event_ids: tuple[str, ...]
    terminal: bool
    status: int | None = None
    retry_after: float | None = None


@dataclass(frozen=True)
class FlushResult:
    """Events delivered and events given up on by one ``flush()`` or ``close()``.

    Events kept because the server was unreachable are in neither count; they are still queued.
    """

    sent: int = 0
    failed: int = 0


Outcome = Literal["sent", "kept", "dropped", "discarded"]


def event_ids(events: Sequence[dict[str, Any]]) -> tuple[str, ...]:
    """Distinct ids in order. Commerce lines share one eventId, so it is reported once."""
    seen: dict[str, None] = {}
    for event in events:
        for item in event.get("payload") or ():
            event_id = item.get("eventId") if isinstance(item, dict) else None
            if isinstance(event_id, str):
                seen.setdefault(event_id, None)
    return tuple(seen)


def resend_wait_s(retry_after_ms: int | None) -> float:
    if retry_after_ms is None:
        return RESEND_WAIT_MIN_S
    return min(RESEND_WAIT_MAX_S, max(RESEND_WAIT_MIN_S, retry_after_ms / 1000))


def deliver(
    send: Callable[[], Any],
    *,
    count: int,
    ids: tuple[str, ...],
    sleep: Callable[[float], None],
    deadline: float | None = None,
) -> tuple[Outcome, DeliveryFailure | None]:
    """Apply the table above. ``send`` returning ``False`` means it discarded on purpose."""

    def failure(
        kind: DeliveryKind, error: IntemptApiError, retry_after: float | None = None
    ) -> DeliveryFailure:
        return DeliveryFailure(
            kind=kind,
            event_count=count,
            event_ids=ids,
            terminal=kind != "transport",
            status=error.status,
            retry_after=retry_after,
        )

    try:
        return ("discarded" if send() is False else "sent"), None
    except IntemptApiError as first:
        error = first

    if not error.reached:
        return "kept", failure("transport", error)
    if error.status not in RESEND_STATUSES:
        return "dropped", failure("terminal" if error.status is not None else "dropped", error)

    wait = resend_wait_s(error.retry_after_ms)
    # A wait that outlives close()'s budget would burn the remaining time and give up anyway.
    past_deadline = deadline is not None and time.monotonic() + wait >= deadline
    if wait > SERVER_WAIT_CAP_S or past_deadline:
        return "dropped", failure("dropped", error, wait)
    sleep(wait)
    try:
        return ("discarded" if send() is False else "sent"), None
    except IntemptApiError as second:
        return "dropped", failure("terminal", second, _advised_s(second))


def _advised_s(error: IntemptApiError) -> float | None:
    if error.status in RESEND_STATUSES:
        return resend_wait_s(error.retry_after_ms)
    return None
