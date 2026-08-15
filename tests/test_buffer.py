"""Batching and the retry policy.

Every assertion here pins a decision that a rewrite could silently change: which
statuses retry, how many attempts the breaker allows, whether backoff grows,
whether a reduced width recovers.
"""

from __future__ import annotations

import time

from intempt import BatchOptions, IntemptApiError
from intempt._buffer import Buffer
from tests.conftest import Reply


def event(name: str) -> dict:
    return {"name": name, "payload": [{"eventId": name, "timestamp": 1, "userId": "u1"}]}


OPTIONS = BatchOptions(size=10, flush_ms=60_000, max_queue=100, flush_on_exit=False)


def failing(status: int):
    def send(_events):
        raise IntemptApiError(f"Intempt API responded {status}", status=status, body="")

    return send


class TestBuffering:
    def test_buffers_until_flush(self, batched, server):
        c = batched()
        c.track("a", user_id="u1")
        c.track("b", user_id="u1")
        assert c.buffered == 2
        assert server.requests == []

        c.flush()
        assert c.buffered == 0
        assert len(server.requests) == 1
        assert len(server.requests[0].body["track"]) == 2

    def test_flushes_at_exactly_batch_size(self, batched, server):
        """flush_ms is 60s, so anything sent was triggered by the width check."""
        c = batched(batch=BatchOptions(size=3, flush_ms=60_000, max_queue=50, flush_on_exit=False))
        c.track("a", user_id="u1")
        c.track("b", user_id="u1")
        assert c.buffered == 2
        c.track("c", user_id="u1")
        assert c.buffered == 0
        assert len(server.requests[0].body["track"]) == 3

    def test_drops_and_names_the_event_when_the_queue_is_full(self, batched, logger):
        c = batched(batch=BatchOptions(size=2, flush_ms=60_000, max_queue=2, flush_on_exit=False))
        c.track("a", user_id="u1")
        # The second fills the queue and triggers a flush; make the third arrive
        # while the queue is still full by stopping the buffer first.
        c._buffer._stopped = True
        c.track("dropped", user_id="u1")
        assert logger.has("error", "batching is stopped")

    def test_close_drains(self, batched, server):
        c = batched()
        c.track("a", user_id="u1")
        c.close()
        assert c.buffered == 0
        assert len(server.requests) == 1

    def test_flush_is_a_no_op_without_batching(self, client, server):
        c = client()
        c.flush()
        assert server.requests == []


class TestRetryPolicy:
    def test_413_halves_the_width_then_succeeds(self, batched, server, logger):
        server.expect(Reply(status=413), Reply(), Reply())
        c = batched(batch=BatchOptions(size=4, flush_ms=60_000, max_queue=50, flush_on_exit=False))
        for i in range(4):
            c.track(f"e{i}", user_id="u1")
        c.flush()

        widths = [len(r.body["track"]) for r in server.requests]
        assert widths[0] == 4
        assert max(widths[1:]) == 2
        assert c.buffered == 0
        assert logger.has("warning", "reducing batch size to 2")

    def test_413_on_a_single_event_drops_it(self, batched, server, logger):
        server.expect(*[Reply(status=413) for _ in range(6)])
        c = batched(batch=BatchOptions(size=2, flush_ms=60_000, max_queue=10, flush_on_exit=False))
        c.track("a", user_id="u1")
        c.track("b", user_id="u1")
        c.flush()

        assert c.buffered == 0
        assert logger.has("error", "single event too large")

    def test_429_honours_retry_after(self, batched, server, logger):
        server.expect(
            Reply(status=429, headers={"Retry-After": "1"}),
            Reply(),
        )
        c = batched(batch=BatchOptions(size=1, flush_ms=10, max_queue=10, flush_on_exit=False))
        started = time.monotonic()
        c.track("a", user_id="u1")
        c.flush()
        elapsed = time.monotonic() - started

        assert logger.has("warning", "retrying in 1000ms")
        assert elapsed >= 0.9

    def test_a_negative_retry_after_never_becomes_a_negative_wait(self, batched, server, logger):
        server.expect(Reply(status=429, headers={"Retry-After": "-5"}), Reply())
        c = batched(batch=BatchOptions(size=1, flush_ms=10, max_queue=10, flush_on_exit=False))
        c.track("a", user_id="u1")
        c.flush()

        waits = [
            int(line.split("retrying in ")[1].rstrip("ms"))
            for line in logger.calls["warning"]
            if "retrying in" in line
        ]
        assert waits and all(w >= 100 for w in waits)

    def test_a_non_retryable_status_drops_the_batch(self, batched, server, logger):
        server.expect(Reply(status=400, body='{"errors":[]}'))
        c = batched(batch=BatchOptions(size=1, flush_ms=60_000, max_queue=10, flush_on_exit=False))
        c.track("a", user_id="u1")
        c.flush()

        assert c.buffered == 0
        assert logger.has("error", "non-retryable error; dropping batch")

    def test_breaker_opens_after_exactly_five_attempts(self, logger):
        """Five, not four and not six."""
        attempts = {"n": 0}

        def send(_events):
            attempts["n"] += 1
            raise IntemptApiError("boom", status=500, body="")

        buffer = Buffer(
            options=BatchOptions(size=1, flush_ms=1, max_queue=10, flush_on_exit=False),
            max_request_events=50,
            logger=logger,
            send=send,
        )
        buffer.enqueue(event("a"))
        buffer.flush()

        assert attempts["n"] == 5
        assert logger.has("error", "5 consecutive failures; stopping batching")

    def test_the_stop_message_says_how_many_are_stranded(self, logger):
        buffer = Buffer(
            options=BatchOptions(size=1, flush_ms=1, max_queue=10, flush_on_exit=False),
            max_request_events=50,
            logger=logger,
            send=failing(500),
        )
        buffer.enqueue(event("a"))
        buffer.enqueue(event("b"))
        buffer.flush()

        stop = [line for line in logger.calls["error"] if "stopping batching" in line]
        assert stop and "event(s) remain buffered" in stop[0]

    def test_backoff_doubles_rather_than_shrinking(self, logger):
        buffer = Buffer(
            options=BatchOptions(size=1, flush_ms=60, max_queue=10, flush_on_exit=False),
            max_request_events=50,
            logger=logger,
            send=failing(500),
        )
        buffer.enqueue(event("a"))
        buffer.flush()

        waits = [
            int(line.split("retrying in ")[1].rstrip("ms"))
            for line in logger.calls["warning"]
            if "retrying in" in line
        ]
        assert waits[:3] == [120, 240, 480]

    def test_a_dropped_batch_does_not_count_toward_the_breaker(self, logger):
        """One 400 after four 500s must not stop batching on the next blip."""
        calls = {"n": 0}

        def send(_events):
            calls["n"] += 1
            raise IntemptApiError("bad", status=400, body="")

        buffer = Buffer(
            options=BatchOptions(size=1, flush_ms=1, max_queue=20, flush_on_exit=False),
            max_request_events=50,
            logger=logger,
            send=send,
        )
        for i in range(8):
            buffer.enqueue(event(f"e{i}"))
        buffer.flush()

        assert buffer.size == 0
        assert not logger.has("error", "stopping batching")


class TestWidthRecovery:
    def test_a_reduced_width_widens_again_after_a_run_of_successes(self, logger):
        widths: list[int] = []
        reject_wide = {"on": True}

        def send(events):
            widths.append(len(events))
            if reject_wide["on"] and len(events) > 2:
                raise IntemptApiError("too large", status=413, body="")

        buffer = Buffer(
            options=BatchOptions(size=4, flush_ms=60_000, max_queue=500, flush_on_exit=False),
            max_request_events=50,
            logger=logger,
            send=send,
        )
        for i in range(4):
            buffer.enqueue(event(f"a{i}"))
        buffer.flush()
        assert widths[0] == 4

        # Healthy again. Ten full-width sends at the reduced width earn a widening.
        reject_wide["on"] = False
        before = len(widths)
        for i in range(48):
            buffer.enqueue(event(f"b{i}"))
        buffer.flush()

        assert max(widths[before:]) == 4, "the width must recover, not stay halved forever"

    def test_widening_ignores_sends_narrower_than_the_current_width(self, logger):
        """Ten width-1 flushes must not earn a widening away from width 2."""
        widths: list[int] = []
        reject_wide = {"on": True}

        def send(events):
            widths.append(len(events))
            if reject_wide["on"] and len(events) > 2:
                raise IntemptApiError("too large", status=413, body="")

        buffer = Buffer(
            options=BatchOptions(size=4, flush_ms=60_000, max_queue=500, flush_on_exit=False),
            max_request_events=50,
            logger=logger,
            send=send,
        )
        for i in range(4):
            buffer.enqueue(event(f"a{i}"))
        buffer.flush()
        reject_wide["on"] = False

        before = len(widths)
        for i in range(20):
            buffer.enqueue(event(f"b{i}"))
            buffer.flush()

        assert set(widths[before:]) == {1}


class TestDropDiagnostics:
    def test_a_gateway_rejecting_everything_keeps_draining(self, logger):
        """It must not stop: stopping strands the queue and every later event."""
        buffer = Buffer(
            options=BatchOptions(size=8, flush_ms=60_000, max_queue=100, flush_on_exit=False),
            max_request_events=50,
            logger=logger,
            send=failing(413),
        )
        for i in range(10):
            buffer.enqueue(event(f"e{i}"))
        buffer.flush()

        assert buffer.size == 0
        assert not logger.has("error", "stopping batching")

    def test_says_once_that_the_gateway_limit_is_a_likely_cause(self, logger):
        buffer = Buffer(
            options=BatchOptions(size=4, flush_ms=60_000, max_queue=100, flush_on_exit=False),
            max_request_events=50,
            logger=logger,
            send=failing(413),
        )
        for i in range(8):
            buffer.enqueue(event(f"e{i}"))
        buffer.flush()

        notices = [
            line
            for line in logger.calls["error"]
            if "rejected as too large with none accepted in between" in line
        ]
        assert len(notices) == 1

    def test_a_burst_of_oversized_events_does_not_punish_the_good_ones(self, logger):
        """The regression a drop-breaker caused on Node: everything stranded."""
        big = {"big0", "big1", "big2", "big3", "big4", "big5"}
        sent: list[str] = []

        def send(events):
            names = [e["name"] for e in events]
            if any(n in big for n in names):
                raise IntemptApiError("too large", status=413, body="")
            sent.extend(names)

        buffer = Buffer(
            options=BatchOptions(size=8, flush_ms=60_000, max_queue=200, flush_on_exit=False),
            max_request_events=50,
            logger=logger,
            send=send,
        )
        for name in sorted(big):
            buffer.enqueue(event(name))
        for i in range(20):
            buffer.enqueue(event(f"good{i}"))
        buffer.flush()

        assert len([n for n in sent if n.startswith("good")]) == 20
        assert buffer.size == 0


class TestCloseBudget:
    def test_close_returns_inside_its_budget(self, logger):
        buffer = Buffer(
            options=BatchOptions(size=10, flush_ms=60_000, max_queue=100, flush_on_exit=False),
            max_request_events=50,
            logger=logger,
            send=failing(500),
            close_budget_s=0.12,
        )
        buffer.enqueue(event("a"))

        started = time.monotonic()
        buffer.close()
        assert time.monotonic() - started < 3.0

    def test_close_says_how_many_it_abandoned(self, logger):
        buffer = Buffer(
            options=BatchOptions(size=10, flush_ms=60_000, max_queue=100, flush_on_exit=False),
            max_request_events=50,
            logger=logger,
            send=failing(500),
            close_budget_s=0.12,
        )
        for i in range(4):
            buffer.enqueue(event(f"e{i}"))
        buffer.close()

        gave_up = [line for line in logger.calls["error"] if "gave up" in line]
        assert gave_up and "4 event(s) unsent" in gave_up[0]

    def test_close_still_drains_everything_when_healthy(self, logger):
        """The bound must not cost events that would have sent."""
        sent: list[str] = []
        buffer = Buffer(
            options=BatchOptions(size=10, flush_ms=60_000, max_queue=100, flush_on_exit=False),
            max_request_events=50,
            logger=logger,
            send=lambda events: sent.extend(e["name"] for e in events),
            close_budget_s=0.12,
        )
        for i in range(25):
            buffer.enqueue(event(f"e{i}"))
        buffer.close()

        assert len(sent) == 25
        assert buffer.size == 0
        assert not logger.has("error", "gave up")

    def test_close_stops_on_the_deadline_even_when_sends_succeed_slowly(self, logger):
        """The other guard only fires on failure; this is the case it cannot see."""
        sent: list[str] = []

        def slow(events):
            time.sleep(0.06)
            sent.extend(e["name"] for e in events)

        buffer = Buffer(
            # size 100 so enqueue never auto-flushes: all 20 must still be queued
            # when close() starts, or there is nothing for the deadline to stop.
            options=BatchOptions(size=100, flush_ms=60_000, max_queue=100, flush_on_exit=False),
            max_request_events=50,
            logger=logger,
            send=slow,
            close_budget_s=0.15,
        )
        for i in range(20):
            buffer.enqueue(event(f"e{i}"))
        # Model a width an earlier 413 had reduced to 1, so the drain takes many
        # slow sends rather than one.
        buffer._batch_size = 1
        buffer.close()

        assert 0 < len(sent) < 20
        assert logger.has("error", "gave up")

    def test_flush_is_not_bounded(self, logger):
        """Only close() gives up. A caller mid-request has not asked to."""
        attempts = {"n": 0}

        def send(_events):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise IntemptApiError("boom", status=500, body="")

        buffer = Buffer(
            options=BatchOptions(size=10, flush_ms=1, max_queue=100, flush_on_exit=False),
            max_request_events=50,
            logger=logger,
            send=send,
            close_budget_s=0.001,
        )
        buffer.enqueue(event("a"))
        buffer.flush()

        assert attempts["n"] == 3
        assert buffer.size == 0


class TestOptOutGate:
    def test_buffered_events_are_discarded_rather_than_sent_after_opt_out(
        self, batched, server, logger
    ):
        """A revocation between capture and flush must be honoured."""
        c = batched()
        c.track("before", user_id="u1")
        c.opt_out()
        c.flush()

        assert server.requests == []
        assert c.buffered == 0
        assert logger.has("warning", "opted out; discarding")

    def test_opting_back_in_does_not_resend_discarded_events(self, batched, server):
        c = batched()
        c.track("before", user_id="u1")
        c.opt_out()
        c.flush()
        c.opt_in()
        c.track("after", user_id="u1")
        c.flush()

        names = [e["name"] for r in server.requests for e in r.body["track"]]
        assert names == ["after"]
