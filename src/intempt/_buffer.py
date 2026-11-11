"""Opt-in event buffer for long-lived processes.

Portions derived from mixpanel-python (Apache License 2.0), as recorded in
NOTICE: the consumer/buffer split and the chunking follow its BufferedConsumer
and Consumer.

Changed substantially. Each batch is sent once under the table in
``_delivery`` (INT-7436): no in-call retry loop, no backoff, no circuit breaker
and no 413 halving, all of which mixpanel-python and earlier versions of this
SDK had. A batch that never reached the server stays queued for the next flush.
A close-initiated drain is bounded.

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
from typing import Any, Callable

from ._config import BatchOptions
from ._delivery import DeliveryFailure, FlushResult, deliver, event_ids

#: How long close() keeps draining before it gives up and reports the loss.
CLOSE_DRAIN_BUDGET_S = 30.0


class Buffer:
    """Queues events and drains them, applying the send policy."""

    def __init__(
        self,
        *,
        options: BatchOptions,
        max_request_events: int,
        logger: Any,
        send: Callable[[list[dict[str, Any]]], Any],
        close_budget_s: float = CLOSE_DRAIN_BUDGET_S,
        sleep: Callable[[float], None] = time.sleep,
        report: Callable[[DeliveryFailure], None] | None = None,
    ) -> None:
        """
        ``sleep`` is injectable so tests can assert the resend wait *decision*
        without paying the wall clock for it.

        ``report`` receives each batch's final failure (INT-7444); the client
        passes one that logs and calls ``on_delivery_failure``.
        """
        self._options = options
        self._logger = logger
        self._send = send
        self._close_budget_s = close_budget_s
        self._sleep = sleep
        self._report = report or (lambda _failure: None)

        self._queue: list[dict[str, Any]] = []
        self._batch_size = min(options.size, max_request_events)
        #: id() of queued events already reported as ``transport``, so an offline
        #: process reports each event once rather than on every flush. Entries leave
        #: with their event, so a recycled id() cannot match a later event.
        self._reported_unreached: set[int] = set()
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

    def enqueue(self, event: dict[str, Any]) -> bool:
        """Buffer an event. False when it was dropped instead, which is also reported."""
        with self._lock:
            if self._stopped:
                reason = "[intempt] batching is stopped; event dropped"
                extra: dict[str, Any] = {"event_name": event.get("name")}
            elif len(self._queue) >= self._options.max_queue:
                reason = "[intempt] batch queue full; event dropped"
                extra = {"event_name": event.get("name"), "max_queue": self._options.max_queue}
            else:
                self._queue.append(event)
                full = len(self._queue) >= self._batch_size
                reason = ""

        if reason:
            self._logger.error(reason, extra=extra)
            self._report(
                DeliveryFailure(
                    kind="dropped", event_count=1, event_ids=event_ids([event]), terminal=True
                )
            )
            return False

        if full:
            self.flush()
        else:
            self._schedule_flush()
        return True

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._queue)

    # -- draining ---------------------------------------------------------

    def flush(self) -> FlushResult:
        """Drain until the queue is empty, the server is unreachable, or the buffer stopped."""
        sent = failed = 0
        with self._drain_lock:
            self._cancel_timer()
            while not self._out_of_close_budget():
                with self._lock:
                    if self._stopped or not self._queue:
                        break
                    batch = self._queue[: self._batch_size]

                outcome, failure = self._attempt(batch)
                if outcome == "kept":
                    self._report_unreached(batch, failure)
                    break

                with self._lock:
                    del self._queue[: len(batch)]
                    for item in batch:
                        self._reported_unreached.discard(id(item))
                if outcome == "sent":
                    sent += len(batch)
                elif outcome == "dropped":
                    failed += len(batch)
                    if failure is not None:
                        self._logger.error(
                            "[intempt] batch dropped",
                            extra={
                                "status": failure.status,
                                "kind": failure.kind,
                                "count": len(batch),
                            },
                        )
                        self._report(failure)
        return FlushResult(sent=sent, failed=failed)

    def _attempt(self, batch: list[dict[str, Any]]) -> tuple[str, DeliveryFailure | None]:
        ids = event_ids(batch)
        try:
            return deliver(
                lambda: self._send(batch),
                count=len(batch),
                ids=ids,
                sleep=self._sleep,
                deadline=self._close_deadline,
            )
        except Exception:
            # Not an answer from the server (e.g. a value json cannot encode), so
            # sending it again cannot help.
            self._logger.error("[intempt] batch could not be sent; dropping", exc_info=True)
            return "dropped", DeliveryFailure(
                kind="dropped", event_count=len(batch), event_ids=ids, terminal=True
            )

    def _report_unreached(
        self, batch: list[dict[str, Any]], failure: DeliveryFailure | None
    ) -> None:
        with self._lock:
            fresh = [item for item in batch if id(item) not in self._reported_unreached]
            self._reported_unreached.update(id(item) for item in fresh)
        self._logger.warning(
            "[intempt] server unreachable; %d event(s) kept for the next flush", len(batch)
        )
        if fresh and failure is not None:
            self._report(
                DeliveryFailure(
                    kind="transport",
                    event_count=len(fresh),
                    event_ids=event_ids(fresh),
                    terminal=False,
                )
            )

    # -- close ------------------------------------------------------------

    def _out_of_close_budget(self) -> bool:
        return self._close_deadline is not None and time.monotonic() >= self._close_deadline

    def close(self) -> FlushResult:
        """Drain within the budget, then drop and report anything left behind."""
        self._close_deadline = time.monotonic() + self._close_budget_s
        try:
            result = self.flush()
        finally:
            self._close_deadline = None

        with self._lock:
            self._stopped = True
            leftover = list(self._queue)
            self._queue.clear()
            self._reported_unreached.clear()
        if leftover:
            self._logger.error(
                "[intempt] close() gave up after %.0fs with %d event(s) unsent.",
                self._close_budget_s,
                len(leftover),
            )
            self._report(
                DeliveryFailure(
                    kind="dropped",
                    event_count=len(leftover),
                    event_ids=event_ids(leftover),
                    terminal=True,
                )
            )
        self._cancel_timer()
        if self._exit_hook is not None:
            with contextlib.suppress(Exception):  # pragma: no cover
                atexit.unregister(self._exit_hook)
            self._exit_hook = None
        return FlushResult(sent=result.sent, failed=result.failed + len(leftover))

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
        # The timer thread must not die: a dead timer stops every later
        # auto-flush. flush() reports its own failures, so nothing is lost here.
        with contextlib.suppress(Exception):  # pragma: no cover
            self.flush()

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
