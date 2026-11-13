"""INT-7444 (CONTRACT-R2 S2): a failed send is reported, never raised.

``track`` answers whether the event was accepted, one ``on_delivery_failure`` listener hears the
final outcome of each batch once, and ``flush()``/``close()`` return ``{sent, failed}``.
"""

from __future__ import annotations

from intempt import BatchOptions, IntemptApiError
from intempt._buffer import Buffer
from tests.conftest import Reply
from tests.test_buffer import RecordedSleep, api, event, scripted


class Heard:
    def __init__(self) -> None:
        self.failures: list = []

    def __call__(self, failure) -> None:
        self.failures.append(failure)

    @property
    def shapes(self) -> list[tuple]:
        return [
            (f.kind, f.status, f.event_count, tuple(f.event_ids), f.retry_after, f.terminal)
            for f in self.failures
        ]


def buffer_with(send, heard, *, size=10, max_queue=100, close_budget_s=30.0):
    return Buffer(
        options=BatchOptions(size=size, flush_ms=60_000, max_queue=max_queue, flush_on_exit=False),
        max_request_events=50,
        logger=_Quiet(),
        send=send,
        sleep=RecordedSleep(),
        close_budget_s=close_budget_s,
        report=heard,
    )


class _Quiet:
    def __getattr__(self, _name):
        return lambda *a, **k: None


def batched_with(batched, heard, **kw):
    return batched(on_delivery_failure=heard, **kw)


class TestListener:
    def test_an_answered_drop_is_reported_once_as_terminal(self, batched, server):
        heard = Heard()
        server.expect(Reply(status=500))
        c = batched_with(batched, heard)
        c.track("a", user_id="u1")
        c.track("b", user_id="u1")
        c.flush()

        ids = tuple(e["payload"][0]["eventId"] for e in server.requests[0].body["track"])
        assert heard.shapes == [("terminal", 500, 2, ids, None, True)]

    def test_a_failed_resend_is_reported_once_not_per_attempt(self):
        heard = Heard()
        buffer = buffer_with(scripted(api(503, 2000), api(503, 2000)), heard)
        buffer.enqueue(event("a"))
        buffer.flush()

        assert heard.shapes == [("terminal", 503, 1, ("a",), 2.0, True)]

    def test_a_wait_past_the_cap_is_reported_as_dropped(self):
        heard = Heard()
        buffer = buffer_with(scripted(api(429, 9000)), heard)
        buffer.enqueue(event("a"))
        buffer.flush()

        assert heard.shapes == [("dropped", 429, 1, ("a",), 9.0, True)]

    def test_a_timeout_after_the_write_is_reported_as_dropped(self):
        heard = Heard()
        buffer = buffer_with(scripted(IntemptApiError("timed out", timed_out=True)), heard)
        buffer.enqueue(event("a"))
        buffer.flush()

        assert heard.shapes == [("dropped", None, 1, ("a",), None, True)]

    def test_an_unreached_batch_is_reported_once_across_flushes(self):
        heard = Heard()
        down = IntemptApiError("refused", reached=False)
        buffer = buffer_with(scripted(down, down, down), heard)
        buffer.enqueue(event("a"))
        buffer.flush()
        buffer.flush()
        buffer.flush()

        assert heard.shapes == [("transport", None, 1, ("a",), None, False)]

    def test_events_queued_behind_an_unreached_batch_get_their_own_report(self):
        heard = Heard()
        down = IntemptApiError("refused", reached=False)
        buffer = buffer_with(scripted(down, down), heard)
        buffer.enqueue(event("a"))
        buffer.flush()
        buffer.enqueue(event("b"))
        buffer.flush()

        assert [f.event_ids for f in heard.failures] == [("a",), ("b",)]

    def test_a_queue_overflow_is_reported_as_dropped(self):
        heard = Heard()
        buffer = buffer_with(lambda events: None, heard, size=5, max_queue=5)
        for name in "abcde":
            buffer._queue.append(event(name))
        assert buffer.enqueue(event("over")) is False

        assert heard.shapes == [("dropped", None, 1, ("over",), None, True)]

    def test_commerce_lines_sharing_an_event_id_report_it_once(self, batched, server):
        heard = Heard()
        server.expect(Reply(status=400))
        c = batched_with(batched, heard)
        c.ecommerce.ordered(user_id="u1", products=[{"product_id": "p1"}, {"product_id": "p2"}])
        c.flush()

        assert heard.failures[0].event_count == 1
        assert len(heard.failures[0].event_ids) == 1

    def test_a_listener_that_raises_is_caught_and_logged(self, batched, server, logger):
        def boom(_failure):
            raise RuntimeError("listener bug")

        server.expect(Reply(status=500))
        c = batched(on_delivery_failure=boom)
        c.track("a", user_id="u1")
        c.flush()

        assert logger.has("error", "on_delivery_failure raised")

    def test_set_config_replaces_the_listener(self, batched, server):
        heard = Heard()
        server.expect(Reply(status=500))
        c = batched()
        c.set_config(on_delivery_failure=heard)
        c.track("a", user_id="u1")
        c.flush()

        assert [f.status for f in heard.failures] == [500]


class TestFlushResult:
    def test_flush_returns_sent_and_failed(self, batched, server):
        server.expect(Reply(), Reply(status=400))
        c = batched(batch=BatchOptions(size=2, flush_ms=60_000, max_queue=50, flush_on_exit=False))
        c._buffer._batch_size = 2
        for name in ("a", "b", "c"):
            c._buffer._queue.append(c._build_event(name, {"user_id": "u1"}))
        result = c.flush()

        assert (result.sent, result.failed) == (2, 1)

    def test_flush_without_batching_returns_zeroes(self, client):
        result = client().flush()
        assert (result.sent, result.failed) == (0, 0)

    def test_close_counts_what_it_could_not_send_as_failed_and_reports_it(self):
        heard = Heard()
        down = IntemptApiError("refused", reached=False)
        buffer = buffer_with(scripted(down), heard)
        buffer.enqueue(event("a"))
        buffer.enqueue(event("b"))
        result = buffer.close()

        assert (result.sent, result.failed) == (0, 2)
        assert [f.kind for f in heard.failures] == ["transport", "dropped"]
        assert heard.failures[-1].event_count == 2

    def test_client_close_returns_the_result(self, batched, server):
        c = batched()
        c.track("a", user_id="u1")
        result = c.close()
        assert (result.sent, result.failed) == (1, 0)


class TestNeverRaises:
    def test_direct_track_reports_instead_of_raising(self, client, server):
        heard = Heard()
        server.expect(Reply(status=500))
        c = client(on_delivery_failure=heard)

        assert c.track("a", user_id="u1") is False
        assert [f.kind for f in heard.failures] == ["terminal"]

    def test_direct_track_returns_true_when_delivered(self, client, server):
        assert client().track("a", user_id="u1") is True

    def test_direct_mode_reports_an_unreached_send_as_dropped(self, client):
        heard = Heard()
        c = client(host="127.0.0.1:1", on_delivery_failure=heard)

        assert c.track("a", user_id="u1") is False
        assert heard.shapes[0][0] == "dropped"
        assert heard.shapes[0][-1] is True

    def test_batched_track_returns_whether_the_event_was_queued(self, batched):
        c = batched()
        assert c.track("a", user_id="u1") is True
        c.opt_out()
        assert c.track("b", user_id="u1") is False

    def test_parallel_track_batch_reports_instead_of_raising(self, client, server):
        heard = Heard()
        server.expect(Reply(), Reply(status=500), Reply())
        c = client(max_request_events=1, max_concurrent_requests=3, on_delivery_failure=heard)

        assert c.track_batch([{"event": f"e{i}", "user_id": "u1"} for i in range(3)]) is False
        assert [f.status for f in heard.failures] == [500]

    def test_consent_reports_instead_of_raising(self, client, server):
        heard = Heard()
        server.expect(Reply(status=500))
        c = client(on_delivery_failure=heard)

        assert c.consent.grant(user_id="u1") is False
        assert [(f.kind, f.event_count, f.event_ids) for f in heard.failures] == [
            ("terminal", 1, ())
        ]


class TestConsentIsNeverSilent:
    """R2 (Ro-bot python#16 security:F1): a failed consent write is reported, never thrown and
    never silent. It returns False, reaches ``on_delivery_failure`` once and logs at error."""

    def test_a_refused_revoke_returns_false_reports_once_and_logs_error(
        self, client, server, logger
    ):
        heard = Heard()
        server.expect(Reply(status=500))
        c = client(on_delivery_failure=heard)

        assert c.consent.revoke(user_id="u1") is False
        assert heard.shapes == [("terminal", 500, 1, (), None, True)]
        assert logger.has("error", "consent.revoke failed")

    def test_a_failure_with_no_listener_still_logs_error(self, client, server, logger):
        server.expect(Reply(status=500))

        assert client().consent.grant(user_id="u1") is False
        assert logger.has("error", "consent.grant failed")

    def test_an_unreached_consent_is_reported_as_dropped(self, client, logger):
        heard = Heard()
        c = client(host="127.0.0.1:1", on_delivery_failure=heard)

        assert c.consent.revoke(user_id="u1") is False
        assert heard.shapes == [("dropped", None, 1, (), None, True)]
        assert logger.has("error", "consent.revoke failed")

    def test_a_second_503_is_reported_once_as_terminal(self, client, server):
        heard = Heard()
        server.expect(Reply(status=503), Reply(status=503))
        c = client(on_delivery_failure=heard)
        c._sleep = lambda _s: None

        assert c.consent.revoke(user_id="u1") is False
        assert len(server.requests) == 2
        assert heard.shapes == [("terminal", 503, 1, (), 1.0, True)]

    def test_an_accepted_consent_whose_body_is_false_is_delivered(self, client, server, logger):
        """The body is the server's, not a discard signal: the record was accepted."""
        heard = Heard()
        server.expect(Reply(body="false"))
        c = client(on_delivery_failure=heard)

        assert c.consent.grant(user_id="u1") is True
        assert heard.failures == []
        assert logger.calls["error"] == []
