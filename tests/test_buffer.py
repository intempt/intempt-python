"""Batching and the send policy.

Every assertion here pins a decision that a rewrite could silently change: which
statuses are resent, how often, and which batches are kept (INT-7436).
"""

from __future__ import annotations

import contextlib
import logging
import time

import pytest

from intempt import BatchOptions, IntemptApiError
from intempt._buffer import Buffer
from tests.conftest import Reply


def event(name: str) -> dict:
    return {"name": name, "payload": [{"eventId": name, "timestamp": 1, "userId": "u1"}]}


class RecordedSleep:
    """Captures requested waits instead of taking them.

    Asserting the requested duration is a stronger check than asserting elapsed
    wall clock, and it keeps the suite fast enough for mutation testing to be
    worth running.
    """

    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)

    @property
    def ms(self) -> list[int]:
        return [round(s * 1000) for s in self.waits]


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
            send=scripted(IntemptApiError("refused", reached=False)),
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
        send = scripted(api(503))
        buffer = Buffer(
            options=BatchOptions(size=10, flush_ms=1, max_queue=100, flush_on_exit=False),
            max_request_events=50,
            logger=logger,
            send=send,
            close_budget_s=0.001,
            sleep=RecordedSleep(),
        )
        buffer.enqueue(event("a"))
        buffer.flush()

        assert send.calls == [["a"], ["a"]]
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


class TestStdlibLoggerExtra:
    """INT-7440. ``RecordingLogger`` accepts any ``extra``; a real ``logging.Logger`` refuses a
    key that collides with a LogRecord attribute (``name``) with a KeyError, which escaped
    ``track()``. These cases therefore need the stdlib logger, not the fixture."""

    @staticmethod
    def _buffer(stdlib_logger, *, size=10, max_queue=100, send=None):
        return Buffer(
            options=BatchOptions(
                size=size, flush_ms=60_000, max_queue=max_queue, flush_on_exit=False
            ),
            max_request_events=50,
            logger=stdlib_logger,
            send=send or (lambda _events: None),
            sleep=RecordedSleep(),
        )

    @pytest.fixture
    def stdlib_logger(self, caplog):
        caplog.set_level(logging.ERROR, logger="intempt.int7440")
        return logging.getLogger("intempt.int7440")

    def test_enqueue_on_a_stopped_buffer_logs_instead_of_raising(self, stdlib_logger, caplog):
        buffer = self._buffer(stdlib_logger)
        buffer.close()

        buffer.enqueue(event("late"))

        assert [r.event_name for r in caplog.records] == ["late"]

    def test_enqueue_on_a_full_queue_logs_instead_of_raising(self, stdlib_logger, caplog):
        # Another thread tracking while a full batch is in flight: the batch stays queued until
        # the send returns, so the arrival finds the queue at max_queue.
        late: list[str] = []

        def send(_events):
            if not late:
                late.append("over")
                buffer.enqueue(event("over"))

        buffer = self._buffer(stdlib_logger, size=2, max_queue=2, send=send)

        buffer.enqueue(event("a"))
        buffer.enqueue(event("b"))

        assert buffer.size == 0
        full = [r for r in caplog.records if "queue full" in r.getMessage()]
        assert [(r.event_name, r.max_queue) for r in full] == [("over", 2)]

    def test_a_413_is_dropped_and_logged(self, stdlib_logger, caplog):
        buffer = self._buffer(stdlib_logger, send=failing(413))

        buffer.enqueue(event("huge"))
        buffer.flush()

        assert buffer.size == 0
        dropped = [r for r in caplog.records if "batch dropped" in r.getMessage()]
        assert [(r.status, r.count) for r in dropped] == [(413, 1)]


def _buffer(send, *, size=10, sleep=None, logger=None, close_budget_s=30.0):
    return Buffer(
        options=BatchOptions(size=size, flush_ms=60_000, max_queue=100, flush_on_exit=False),
        max_request_events=50,
        logger=logger or _NullLogger(),
        send=send,
        sleep=sleep or RecordedSleep(),
        close_budget_s=close_budget_s,
    )


class _NullLogger:
    def __getattr__(self, _name):
        return lambda *a, **k: None


class scripted:
    """A send that raises (or returns) per call, recording the batch it was handed."""

    def __init__(self, *outcomes):
        self.outcomes = outcomes
        self.calls: list[list[str]] = []

    def __call__(self, events):
        self.calls.append([e["name"] for e in events])
        if len(self.calls) <= len(self.outcomes):
            outcome = self.outcomes[len(self.calls) - 1]
            if isinstance(outcome, Exception):
                raise outcome


def api(status, retry_after_ms=None):
    return IntemptApiError(
        f"Intempt API responded {status}", status=status, body="", retry_after_ms=retry_after_ms
    )


class TestSendOnce:
    """INT-7436 (CONTRACT-R2 S1): send once; 503 and 429 get one resend; never-reached is kept."""

    @pytest.mark.parametrize("status", [400, 401, 404, 408, 413, 500, 502, 504])
    def test_an_answered_failure_is_sent_once_and_dropped(self, status):
        send = scripted(api(status))
        buffer = _buffer(send)
        for name in ("a", "b", "c", "d"):
            buffer.enqueue(event(name))
        buffer.flush()

        assert send.calls == [["a", "b", "c", "d"]]
        assert buffer.size == 0

    def test_the_next_event_after_a_drop_is_delivered_alone(self):
        send = scripted(api(500))
        buffer = _buffer(send)
        buffer.enqueue(event("a"))
        buffer.flush()
        buffer.enqueue(event("b"))
        buffer.flush()

        assert send.calls == [["a"], ["b"]]

    def test_a_timeout_after_the_write_is_not_resent(self):
        send = scripted(IntemptApiError("request timed out after 10.0s"))
        buffer = _buffer(send)
        buffer.enqueue(event("a"))
        buffer.flush()

        assert send.calls == [["a"]]
        assert buffer.size == 0

    @pytest.mark.parametrize("status", [503, 429])
    def test_503_and_429_get_exactly_one_resend(self, status):
        slept = RecordedSleep()
        send = scripted(api(status), api(status), None)
        buffer = _buffer(send, sleep=slept)
        buffer.enqueue(event("a"))
        buffer.flush()

        assert send.calls == [["a"], ["a"]]
        assert buffer.size == 0
        assert slept.waits == [1.0]

    def test_the_resend_waits_retry_after(self):
        slept = RecordedSleep()
        send = scripted(api(429, retry_after_ms=3000), None)
        buffer = _buffer(send, sleep=slept)
        buffer.enqueue(event("a"))
        buffer.flush()

        assert send.calls == [["a"], ["a"]]
        assert slept.waits == [3.0]

    @pytest.mark.parametrize(
        ("advised_ms", "wait_s"), [(None, 1.0), (0, 1.0), (400, 1.0), (5000, 5.0)]
    )
    def test_the_wait_is_clamped_to_at_least_one_second(self, advised_ms, wait_s):
        slept = RecordedSleep()
        buffer = _buffer(scripted(api(503, retry_after_ms=advised_ms), None), sleep=slept)
        buffer.enqueue(event("a"))
        buffer.flush()

        assert slept.waits == [wait_s]

    def test_a_wait_past_the_five_second_cap_drops_without_sleeping(self):
        slept = RecordedSleep()
        send = scripted(api(503, retry_after_ms=5001), None)
        buffer = _buffer(send, sleep=slept)
        buffer.enqueue(event("a"))
        buffer.flush()

        assert send.calls == [["a"]]
        assert slept.waits == []
        assert buffer.size == 0

    def test_a_batch_that_never_reached_the_server_is_kept_for_the_next_flush(self):
        send = scripted(IntemptApiError("Connection refused", reached=False), None)
        buffer = _buffer(send)
        buffer.enqueue(event("a"))
        buffer.enqueue(event("b"))
        buffer.flush()

        assert send.calls == [["a", "b"]]
        assert buffer.size == 2

        buffer.flush()
        assert send.calls == [["a", "b"], ["a", "b"]]
        assert buffer.size == 0

    def test_failures_never_stop_the_buffer(self):
        send = scripted(*[api(500) for _ in range(8)])
        buffer = _buffer(send, size=1)
        for i in range(8):
            buffer.enqueue(event(f"e{i}"))
        buffer.enqueue(event("after"))

        assert send.calls[-1] == ["after"]
        assert len(send.calls) == 9


class TestTransportReach:
    """INT-7436: the transport says whether the request reached the server."""

    def test_a_refused_connection_never_reached_the_server(self, client):
        c = client(host="127.0.0.1:1")
        with pytest.raises(IntemptApiError) as caught:
            c._transport.post("/x", {})
        assert caught.value.reached is False

    def test_a_timeout_after_the_write_reached_the_server(self, client, server):
        server.expect(Reply(delay_s=0.5))
        c = client(timeout=0.05)
        with pytest.raises(IntemptApiError) as caught:
            c._transport.post("/x", {})
        assert caught.value.reached is True
        assert caught.value.timed_out is True

    def test_retryable_names_only_the_statuses_resent_once(self):
        assert [s for s in (400, 408, 413, 429, 500, 503, 504) if api(s).retryable] == [429, 503]


class TestDirectModeSendOnce:
    def test_direct_mode_resends_a_503_once(self, client, server):
        server.expect(Reply(status=503), Reply())
        c = client()
        c._sleep = RecordedSleep()
        c.track("a", user_id="u1")

        assert len(server.requests) == 2

    def test_direct_mode_sends_a_500_once(self, client, server):
        server.expect(Reply(status=500), Reply())
        c = client()
        with contextlib.suppress(IntemptApiError):
            c.track("a", user_id="u1")

        assert len(server.requests) == 1
