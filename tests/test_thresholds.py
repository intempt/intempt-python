"""The constants are the policy, so each one is pinned to its exact value.

Mutation testing found every threshold in `_buffer` survivable: 10 minutes could
become 11, the 100ms floor could become 101, "widen after 10 successes" could
become 11, and nothing failed. A threshold nothing asserts is a number somebody
can change by accident, and the retry table is the part of this SDK where a
quiet change costs delivered events.
"""

from __future__ import annotations

from intempt import BatchOptions, IntemptApiError
from intempt._buffer import (
    CLOSE_DRAIN_BUDGET_S,
    DROPS_BEFORE_WARNING,
    MAX_CONSECUTIVE_FAILURES,
    MAX_RETRY_INTERVAL_MS,
    MIN_RETRY_INTERVAL_MS,
    SUCCESSES_BEFORE_WIDENING,
    Buffer,
)
from tests.test_buffer import RecordedSleep, event, failing


def options(**kw) -> BatchOptions:
    base = {"size": 10, "flush_ms": 60_000, "max_queue": 500, "flush_on_exit": False}
    base.update(kw)
    return BatchOptions(**base)


class TestDeclaredValues:
    """The documented numbers, asserted against the README and ARCHITECTURE."""

    def test_the_constants_are_what_the_docs_claim(self):
        assert MIN_RETRY_INTERVAL_MS == 100
        assert MAX_RETRY_INTERVAL_MS == 10 * 60 * 1000
        assert MAX_CONSECUTIVE_FAILURES == 5
        assert DROPS_BEFORE_WARNING == 3
        assert SUCCESSES_BEFORE_WIDENING == 10
        assert CLOSE_DRAIN_BUDGET_S == 30.0


class TestBackoffBounds:
    def test_the_floor_is_exactly_100ms(self, logger):
        """flush_ms of 1 computes a 2ms backoff, which the floor must lift to 100."""
        slept = RecordedSleep()
        buffer = Buffer(
            options=options(size=1, flush_ms=1, max_queue=10),
            max_request_events=50,
            logger=logger,
            send=failing(500),
            sleep=slept,
        )
        buffer.enqueue(event("a"))
        buffer.flush()

        assert slept.ms[0] == 100

    def test_the_cap_is_exactly_10_minutes(self, logger):
        """A hostile Retry-After must clamp to the cap, not to something near it."""
        slept = RecordedSleep()

        def send(_events):
            raise IntemptApiError("slow down", status=429, body="", retry_after_ms=999_999_999)

        buffer = Buffer(
            options=options(size=1, flush_ms=10, max_queue=10),
            max_request_events=50,
            logger=logger,
            send=send,
            sleep=slept,
        )
        buffer.enqueue(event("a"))
        buffer.flush()

        assert slept.ms[0] == 600_000

    def test_a_computed_backoff_between_the_bounds_is_untouched(self):
        """Neither bound may clamp a value that is already reasonable."""
        assert MIN_RETRY_INTERVAL_MS < 480 < MAX_RETRY_INTERVAL_MS


class TestWideningThreshold:
    def test_widens_on_the_tenth_success_and_not_the_ninth(self, logger):
        widths: list[int] = []
        reject_wide = {"on": True}

        def send(events):
            widths.append(len(events))
            if reject_wide["on"] and len(events) > 2:
                raise IntemptApiError("too large", status=413, body="")

        buffer = Buffer(
            options=options(size=4),
            max_request_events=50,
            logger=logger,
            send=send,
            sleep=RecordedSleep(),
        )
        # One 413 takes the width from 4 to 2.
        for i in range(4):
            buffer.enqueue(event(f"a{i}"))
        buffer.flush()
        reject_wide["on"] = False

        # Phase one drained the remaining two events at width 2, which is two
        # successes on the streak. Eight more full-width sends reach nine.
        for round_ in range(7):
            for k in range(2):
                buffer.enqueue(event(f"b{round_}-{k}"))
            buffer.flush()

        before = len(widths)
        for k in range(2):
            buffer.enqueue(event(f"c{k}"))
        buffer.flush()
        assert max(widths[before:]) == 2, "must not widen before the tenth success"

        # The tenth success widens, which the next send shows.
        for k in range(2):
            buffer.enqueue(event(f"d{k}"))
        buffer.flush()
        for k in range(4):
            buffer.enqueue(event(f"e{k}"))
        buffer.flush()
        assert max(widths) == 4, "must widen once the tenth full-width success lands"


class TestDropWarningThreshold:
    def test_the_notice_fires_on_the_third_drop_not_the_fourth(self, logger):
        buffer = Buffer(
            options=options(size=1, max_queue=10),
            max_request_events=50,
            logger=logger,
            send=failing(413),
            sleep=RecordedSleep(),
        )

        def notices() -> int:
            return len(
                [
                    line
                    for line in logger.calls["error"]
                    if "rejected as too large with none accepted in between" in line
                ]
            )

        for i in range(2):
            buffer.enqueue(event(f"e{i}"))
            buffer.flush()
        assert notices() == 0, "must not fire before the third drop"

        buffer.enqueue(event("third"))
        buffer.flush()
        assert notices() == 1, "must fire on exactly the third"

    def test_the_drop_tally_starts_at_zero(self, logger):
        """Starting at 1 would fire the notice a drop early."""
        buffer = Buffer(
            options=options(size=1, max_queue=10),
            max_request_events=50,
            logger=logger,
            send=failing(413),
            sleep=RecordedSleep(),
        )
        buffer.enqueue(event("only"))
        buffer.flush()
        assert not any("rejected as too large" in line for line in logger.calls["error"]), (
            "one drop must not reach a threshold of three"
        )


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


class TestBreakerThreshold:
    def test_the_failure_tally_starts_at_zero(self, logger):
        """Starting at 1 would trip the breaker an attempt early."""
        attempts = {"n": 0}

        def send(_events):
            attempts["n"] += 1
            raise IntemptApiError("boom", status=500, body="")

        buffer = Buffer(
            options=options(size=1, flush_ms=1, max_queue=10),
            max_request_events=50,
            logger=logger,
            send=send,
            sleep=RecordedSleep(),
        )
        buffer.enqueue(event("a"))
        buffer.flush()
        assert attempts["n"] == MAX_CONSECUTIVE_FAILURES
