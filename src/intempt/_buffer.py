"""Opt-in event buffer for long-lived processes.

Portions derived from mixpanel-python (Apache License 2.0), as recorded in
NOTICE: the consumer/buffer split, the chunking and the retry-with-backoff loop
follow its BufferedConsumer and Consumer.

Changed substantially. The retry policy is Intempt's: a 413 halves the batch
width and recovers by doubling, 429 honours Retry-After, a circuit breaker opens
after five consecutive failures, and a close-initiated drain is bounded.
mixpanel-python retries on a fixed schedule with no width adaptation and no
breaker.

Deliberately in memory. Crash durability needs disk with fsync, file locking and
boot-time recovery, which is a different design and is not in scope.

Copyright 2026 Intempt Technologies
Licensed under the Apache License, Version 2.0.
"""

from __future__ import annotations

import atexit
import contextlib
import threading
import time
from collections.abc import Sequence
from typing import Any, Callable

from ._config import BatchOptions
from ._errors import IntemptApiError

MAX_RETRY_INTERVAL_MS = 10 * 60 * 1000
#: Floor for any retry, so a zero or past Retry-After cannot become a hot loop.
MIN_RETRY_INTERVAL_MS = 100
MAX_CONSECUTIVE_FAILURES = 5

#: Consecutive single-event 413 drops before saying so once.
#:
#: Diagnostic only. Using this tally to change behaviour was tried twice on the
#: Node SDK and both attempts were worse than what they fixed: stopping stranded
#: the queue and discarded every later event, and pinning the width to 1 capped
#: throughput to one event per round trip so a fast producer overflowed the
#: queue. Trading delivered events for a lower request count is the wrong
#: direction.
DROPS_BEFORE_WARNING = 3

#: Successful full-width sends before trying a wider batch again.
SUCCESSES_BEFORE_WIDENING = 10

#: How long close() keeps draining before it gives up and reports the loss.
CLOSE_DRAIN_BUDGET_S = 30.0


class Buffer:
    """Queues events and drains them, applying the retry policy."""

    def __init__(
        self,
        *,
        options: BatchOptions,
        max_request_events: int,
        logger: Any,
        send: Callable[[list[dict[str, Any]]], None],
        close_budget_s: float = CLOSE_DRAIN_BUDGET_S,
    ) -> None:
        self._options = options
        self._max_request_events = max_request_events
        self._logger = logger
        self._send = send
        self._close_budget_s = close_budget_s

        self._queue: list[dict[str, Any]] = []
        self._batch_size = min(options.size, max_request_events)
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._consecutive_drops = 0
        self._stopped = False
        self._close_deadline: float | None = None

        # One lock guards the queue; one guards draining, so two callers can
        # never drain the same slice.
        self._lock = threading.Lock()
        self._drain_lock = threading.RLock()

        self._timer: threading.Timer | None = None
        self._exit_hook: Callable[[], None] | None = None
        if options.flush_on_exit:
            self._exit_hook = self._on_exit
            atexit.register(self._exit_hook)

    # -- queueing ---------------------------------------------------------

    def enqueue(self, event: dict[str, Any]) -> None:
        """Buffer an event, or log and drop it.

        Returns nothing on purpose: a drop is reported through the logger, which
        is the channel callers actually have.
        """
        with self._lock:
            if self._stopped:
                self._logger.error(
                    "[intempt] batching is stopped; event dropped",
                    extra={"name": event.get("name")},
                )
                return
            if len(self._queue) >= self._options.max_queue:
                self._logger.error(
                    "[intempt] batch queue full; event dropped",
                    extra={"name": event.get("name"), "max_queue": self._options.max_queue},
                )
                return
            self._queue.append(event)
            full = len(self._queue) >= self._batch_size

        if full:
            self.flush()
        else:
            self._schedule_flush()

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._queue)

    # -- draining ---------------------------------------------------------

    def flush(self) -> None:
        """Drain until the queue is empty or the batcher has stopped."""
        with self._drain_lock:
            self._cancel_timer()
            while True:
                if self._out_of_close_budget():
                    return
                with self._lock:
                    if self._stopped or not self._queue:
                        return
                    batch = self._queue[: self._batch_size]

                try:
                    self._send(batch)
                except Exception as exc:  # noqa: BLE001 - classified below
                    action = self._handle_failure(exc, batch)
                    if action == "requeue":
                        continue
                    return

                with self._lock:
                    del self._queue[: len(batch)]
                self._consecutive_failures = 0
                self._consecutive_drops = 0
                self._widen_if_earned(len(batch))

    def _widen_if_earned(self, sent: int) -> None:
        """Grow the width back after a run of successes at the current width.

        Comparing against the full width instead would be unreachable: the batch
        is sliced to the current width, so once a 413 halves it the condition can
        never be true again and the reduction lasts for the life of the client.

        Only a send that filled the current width counts, so a trickle producer
        does not earn a widening from batches that never tested the width. At a
        width of 1 that filter cannot bite, which is the intended floor.
        """
        full = min(self._options.size, self._max_request_events)
        if self._batch_size < full and sent >= self._batch_size:
            self._consecutive_successes += 1
            if self._consecutive_successes >= SUCCESSES_BEFORE_WIDENING:
                self._batch_size = min(full, self._batch_size * 2)
                self._consecutive_successes = 0

    def _handle_failure(self, error: Exception, batch: Sequence[dict[str, Any]]) -> str:
        """Apply the retry table. Returns 'requeue' or 'stop'.

        413 batch > 1   halve the width and retry
        413 batch = 1   drop the event, log it, return the width to full
        429             honour Retry-After, else exponential backoff
        5xx/408/timeout exponential backoff
        other 4xx       drop the batch, surface status and body
        """
        api_error = error if isinstance(error, IntemptApiError) else None
        status = api_error.status if api_error else None

        # Any failure ends the run of successes, whichever branch handles it.
        self._consecutive_successes = 0

        if status == 413:
            return self._handle_too_large(batch)

        if api_error is not None and not api_error.retryable:
            self._logger.error(
                "[intempt] non-retryable error; dropping batch",
                extra={"status": status, "body": api_error.body, "count": len(batch)},
            )
            with self._lock:
                del self._queue[: len(batch)]
            # Dropping a malformed batch is not a transient failure, so it must
            # not count toward the breaker.
            self._consecutive_failures = 0
            return "requeue"

        self._consecutive_failures += 1
        if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            self._logger.error(
                "[intempt] %d consecutive failures; stopping batching. "
                "%d event(s) remain buffered.",
                self._consecutive_failures,
                self.size,
            )
            self._stopped = True
            return "stop"

        backoff_ms = self._backoff_ms(api_error)
        # Starting a wait that outlives the close budget burns the remaining time
        # and gives up anyway.
        if (
            self._close_deadline is not None
            and time.monotonic() + backoff_ms / 1000 >= self._close_deadline
        ):
            return "stop"
        self._logger.warning("[intempt] send failed; retrying in %dms", backoff_ms)
        time.sleep(backoff_ms / 1000)
        return "requeue"

    def _handle_too_large(self, batch: Sequence[dict[str, Any]]) -> str:
        if len(batch) > 1:
            self._batch_size = max(1, len(batch) // 2)
            self._logger.warning(
                "[intempt] 413 received; reducing batch size to %d", self._batch_size
            )
            return "requeue"

        self._logger.error(
            "[intempt] single event too large; dropping",
            extra={"name": batch[0].get("name") if batch else None},
        )
        with self._lock:
            del self._queue[:1]
        self._consecutive_failures = 0
        # The offending event is gone, so the width was never the problem. Any
        # policy that keeps the width down here costs delivered events, because
        # the widening ramp then has to climb back while the producer keeps
        # filling the queue.
        self._batch_size = min(self._options.size, self._max_request_events)
        self._consecutive_drops += 1
        if self._consecutive_drops == DROPS_BEFORE_WARNING:
            # Hedged deliberately: this tally cannot tell a gateway whose limit
            # is below one event from a burst of individually oversized events,
            # and in the second case everything behind them sends fine.
            self._logger.error(
                "[intempt] %d events rejected as too large with none accepted in "
                "between. Either those events are individually oversized, or the "
                "gateway's request body limit is below a single event — if it is "
                "the latter, every event will be dropped until the limit is raised.",
                self._consecutive_drops,
            )
        return "requeue"

    def _backoff_ms(self, api_error: IntemptApiError | None) -> int:
        advised = None
        if api_error is not None and api_error.retry_after_ms:
            # Only a positive value. A zero or already-past Retry-After arrives
            # here as 0 and would otherwise burn every attempt in milliseconds.
            advised = api_error.retry_after_ms
        computed = self._options.flush_ms * (2**self._consecutive_failures)
        return min(MAX_RETRY_INTERVAL_MS, max(MIN_RETRY_INTERVAL_MS, advised or computed))

    # -- close ------------------------------------------------------------

    def _out_of_close_budget(self) -> bool:
        return self._close_deadline is not None and time.monotonic() >= self._close_deadline

    def close(self) -> None:
        """Drain within the budget, then report anything left behind."""
        self._close_deadline = time.monotonic() + self._close_budget_s
        try:
            self.flush()
        finally:
            self._close_deadline = None

        remaining = self.size
        if remaining:
            self._logger.error(
                "[intempt] close() gave up after %.0fs with %d event(s) unsent.",
                self._close_budget_s,
                remaining,
            )
        self._stopped = True
        self._cancel_timer()
        if self._exit_hook is not None:
            with contextlib.suppress(Exception):  # pragma: no cover
                atexit.unregister(self._exit_hook)
            self._exit_hook = None

    def _on_exit(self) -> None:  # pragma: no cover - exercised by the exit hook
        # An exit hook must never raise: it runs while the interpreter is
        # shutting down and an exception there is reported without context.
        with contextlib.suppress(Exception):
            self.flush()

    # -- timer ------------------------------------------------------------

    def _schedule_flush(self) -> None:
        if self._timer is not None or self._stopped:
            return
        timer = threading.Timer(self._options.flush_ms / 1000, self._on_timer)
        # Never hold the process open just to wait for a flush.
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _on_timer(self) -> None:
        self._timer = None
        # The timer thread must not die on a send failure: the retry policy has
        # already logged it, and a dead timer stops every later auto-flush.
        with contextlib.suppress(Exception):  # pragma: no cover
            self.flush()

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
