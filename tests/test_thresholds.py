"""The constants are the policy, so each one is pinned to its exact value.

A threshold nothing asserts is a number somebody can change by accident. The send table's
numbers come from CONTRACT-R2 S1 (INT-7436): one resend for 503/429 after Retry-After clamped to
[1 s, 60 s], capped at 5 s in a server SDK.
"""

from __future__ import annotations

from intempt import BatchOptions
from intempt._buffer import CLOSE_DRAIN_BUDGET_S, Buffer
from intempt._delivery import (
    RESEND_STATUSES,
    RESEND_WAIT_MAX_S,
    RESEND_WAIT_MIN_S,
    SERVER_WAIT_CAP_S,
    resend_wait_s,
)
from tests.test_buffer import RecordedSleep, event


def options(**kw) -> BatchOptions:
    base = {"size": 10, "flush_ms": 60_000, "max_queue": 500, "flush_on_exit": False}
    base.update(kw)
    return BatchOptions(**base)


class TestDeclaredValues:
    def test_the_constants_are_what_the_contract_says(self):
        assert RESEND_STATUSES == (429, 503)
        assert RESEND_WAIT_MIN_S == 1.0
        assert RESEND_WAIT_MAX_S == 60.0
        assert SERVER_WAIT_CAP_S == 5.0
        assert CLOSE_DRAIN_BUDGET_S == 30.0


class TestResendWait:
    def test_retry_after_is_clamped_to_the_bounds(self):
        assert resend_wait_s(None) == 1.0
        assert resend_wait_s(999) == 1.0
        assert resend_wait_s(1000) == 1.0
        assert resend_wait_s(2500) == 2.5
        assert resend_wait_s(60_000) == 60.0
        assert resend_wait_s(60_001) == 60.0


class TestCloseBudgetDefault:
    def test_the_default_budget_is_thirty_seconds(self, logger):
        """Every other close test injects a budget, so the default is untested."""
        buffer = Buffer(
            options=options(),
            max_request_events=50,
            logger=logger,
            send=lambda events: None,
        )
        assert buffer._close_budget_s == 30.0


class TestQueueBound:
    """max_queue is a cap: the Nth event fits, the N+1th is dropped.

    The cap is only reachable while a flush is in flight. `_send` runs outside
    the queue lock, so a producer can add events while a drain is blocked on the
    network — which is exactly the situation the cap exists for. A single-threaded
    test cannot reach it, because enqueue drains synchronously as soon as the
    batch is full.
    """

    def test_an_event_arriving_at_the_cap_is_dropped_not_queued(self, logger):
        import threading

        entered = threading.Event()
        release = threading.Event()

        def send(events):
            entered.set()
            release.wait(5)

        buffer = Buffer(
            options=options(size=2, flush_ms=60_000, max_queue=2),
            max_request_events=50,
            logger=logger,
            send=send,
            sleep=RecordedSleep(),
        )

        # This thread fills the batch, which triggers a flush that blocks inside
        # send. The two events stay queued until the send returns.
        producer = threading.Thread(
            target=lambda: [buffer.enqueue(event("a")), buffer.enqueue(event("b"))]
        )
        producer.start()
        assert entered.wait(5), "the flush never reached send"

        try:
            assert len(buffer._queue) == 2, "both events are queued during the send"
            assert not any("queue full" in line for line in logger.calls["error"]), (
                "the second event fills the cap and must still have been accepted"
            )

            buffer.enqueue(event("over-the-cap"))

            assert sum("queue full" in line for line in logger.calls["error"]) == 1
            assert len(buffer._queue) == 2, "the cap must not be exceeded"
            names = [item.get("name") for item in buffer._queue]
            assert "over-the-cap" not in names
        finally:
            release.set()
            producer.join(5)

    def test_the_drop_names_the_event_and_the_cap(self, logger):
        import threading

        entered = threading.Event()
        release = threading.Event()

        def send(events):
            entered.set()
            release.wait(5)

        buffer = Buffer(
            options=options(size=1, flush_ms=60_000, max_queue=1),
            max_request_events=50,
            logger=logger,
            send=send,
            sleep=RecordedSleep(),
        )
        producer = threading.Thread(target=lambda: buffer.enqueue(event("kept")))
        producer.start()
        assert entered.wait(5)

        try:
            buffer.enqueue(event("dropped"))

            assert any("queue full" in line for line in logger.calls["error"])
            extras = [
                extra for extra in logger.context["error"] if extra.get("max_queue") is not None
            ]
            assert extras, "the operator needs the cap that was hit"
            assert extras[-1]["max_queue"] == 1
            assert extras[-1]["event_name"] == "dropped"
        finally:
            release.set()
            producer.join(5)
