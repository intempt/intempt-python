"""INT-7444 (CONTRACT-R2 S2): a failed send is reported, never raised.

``track`` answers whether the event was accepted, one ``on_delivery_failure`` listener hears the
final outcome of each batch once, and ``flush()``/``close()`` return ``{sent, failed}``.
"""

from __future__ import annotations

from intempt import BatchOptions, FlushResult, IntemptApiError
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


class TestKeptBatchWaits:
    """R4: a batch that never reached the server waits for the next flush interval.

    Enqueue never starts a network attempt while it waits, the timer is re-armed so it is not
    stranded, and the queue cap evicts the oldest waiting event instead of refusing the newest.
    """

    @staticmethod
    def kept_buffer(send, heard, *, size=2, max_queue=100):
        return buffer_with(send, heard, size=size, max_queue=max_queue)

    def test_enqueue_starts_no_attempt_while_a_kept_batch_waits(self):
        down = IntemptApiError("refused", reached=False)
        send = scripted(down)
        buffer = self.kept_buffer(send, Heard())
        buffer.enqueue(event("a"))
        buffer.enqueue(event("b"))
        assert send.calls == [["a", "b"]]

        assert buffer.enqueue(event("c")) is True
        assert buffer.enqueue(event("d")) is True
        assert send.calls == [["a", "b"]]
        buffer.close()

    def test_an_explicit_flush_still_retries_and_a_delivery_ends_the_wait(self):
        down = IntemptApiError("refused", reached=False)
        send = scripted(down)
        buffer = self.kept_buffer(send, Heard())
        for name in "abcd":
            buffer.enqueue(event(name))

        assert buffer.flush() == FlushResult(sent=4, failed=0)
        buffer.enqueue(event("e"))
        buffer.enqueue(event("f"))
        assert send.calls == [["a", "b"], ["a", "b"], ["c", "d"], ["e", "f"]]
        buffer.close()

    def test_an_answered_drop_also_ends_the_wait(self):
        down = IntemptApiError("refused", reached=False)
        send = scripted(down, api(400))
        buffer = self.kept_buffer(send, Heard())
        buffer.enqueue(event("a"))
        buffer.enqueue(event("b"))
        buffer.flush()

        buffer.enqueue(event("c"))
        buffer.enqueue(event("d"))
        assert send.calls == [["a", "b"], ["a", "b"], ["c", "d"]]
        buffer.close()

    def test_a_kept_flush_rearms_the_timer_at_the_flush_interval(self):
        down = IntemptApiError("refused", reached=False)
        buffer = self.kept_buffer(scripted(down, down), Heard())
        buffer.enqueue(event("a"))
        buffer.enqueue(event("b"))
        try:
            assert buffer._timer is not None
            assert buffer._timer.interval == 60.0
            buffer.flush()
            assert buffer._timer is not None
        finally:
            buffer.close()
        assert buffer._timer is None

    def test_a_delivered_flush_leaves_no_timer_behind(self):
        buffer = self.kept_buffer(scripted(), Heard())
        buffer.enqueue(event("a"))
        buffer.enqueue(event("b"))

        assert buffer._timer is None
        buffer.close()

    def test_the_timer_retries_a_kept_batch(self):
        down = IntemptApiError("refused", reached=False)
        send = scripted(down)
        buffer = self.kept_buffer(send, Heard())
        buffer.enqueue(event("a"))
        buffer.enqueue(event("b"))

        buffer._on_timer()
        assert send.calls == [["a", "b"], ["a", "b"]]
        assert buffer.size == 0
        buffer.close()

    def test_at_the_cap_the_oldest_waiting_event_is_evicted(self, logger):
        heard = Heard()
        send = scripted()
        buffer = buffer_with(send, heard, size=5, max_queue=5)
        buffer._logger = logger
        for name in "abcde":
            buffer._queue.append(event(name))

        assert buffer.enqueue(event("new")) is True
        assert heard.shapes == [("dropped", None, 1, ("a",), None, True)]
        assert logger.has("error", "oldest event dropped")
        assert send.calls == [["b", "c", "d", "e", "new"]]

    def test_an_evicted_kept_event_is_forgotten_by_the_once_only_report(self):
        down = IntemptApiError("refused", reached=False)
        heard = Heard()
        buffer = self.kept_buffer(scripted(down), heard, size=2, max_queue=2)
        buffer.enqueue(event("a"))
        buffer.enqueue(event("b"))

        buffer.enqueue(event("c"))
        assert buffer._reported_unreached == {id(item) for item in buffer._queue[:1]}
        assert [f.event_ids for f in heard.failures] == [("a", "b"), ("a",)]
        buffer.close()

    def test_events_in_flight_are_never_evicted(self):
        import threading

        entered = threading.Event()
        release = threading.Event()
        heard = Heard()
        sent: list[list[str]] = []

        def send(events):
            sent.append([e["name"] for e in events])
            if len(sent) == 1:
                entered.set()
                release.wait(5)

        buffer = buffer_with(send, heard, size=2, max_queue=3)
        producer = threading.Thread(
            target=lambda: [buffer.enqueue(event("a")), buffer.enqueue(event("b"))]
        )
        producer.start()
        assert entered.wait(5)
        try:
            assert buffer.enqueue(event("c")) is True
            assert buffer.enqueue(event("d")) is True
            assert [e["name"] for e in buffer._queue] == ["a", "b", "d"]
            assert [f.event_ids for f in heard.failures] == [("c",)]
        finally:
            release.set()
            producer.join(5)
        assert sent == [["a", "b"], ["d"]]
        assert buffer.size == 0
        buffer.close()

    def test_when_every_queued_event_is_in_flight_the_new_one_is_refused(self):
        import threading

        entered = threading.Event()
        release = threading.Event()
        heard = Heard()

        def send(_events):
            entered.set()
            release.wait(5)

        buffer = buffer_with(send, heard, size=2, max_queue=2)
        producer = threading.Thread(
            target=lambda: [buffer.enqueue(event("a")), buffer.enqueue(event("b"))]
        )
        producer.start()
        assert entered.wait(5)
        try:
            assert buffer.enqueue(event("late")) is False
            assert [f.event_ids for f in heard.failures] == [("late",)]
        finally:
            release.set()
            producer.join(5)
        buffer.close()


class TestWhatAFailureCarries:
    """The listener's shape and the structured log fields are what an operator acts on."""

    def test_a_direct_failure_reports_its_count_ids_and_structured_log(
        self, client, server, logger
    ):
        heard = Heard()
        server.expect(Reply(status=500))
        c = client(on_delivery_failure=heard)

        assert c.track("a", user_id="u1") is False
        event_id = server.requests[0].body["track"][0]["payload"][0]["eventId"]
        assert heard.shapes == [("terminal", 500, 1, (event_id,), None, True)]
        assert logger.has("error", "] send failed; 1 event(s) dropped")
        assert logger.context["error"] == [{"status": 500, "kind": "terminal"}]

    def test_a_listener_that_raises_is_logged_with_its_traceback(self, client, server, caplog):
        import logging

        def listener(_failure):
            raise RuntimeError("listener broke")

        caplog.set_level(logging.ERROR, logger="intempt.listener")
        server.expect(Reply(status=500))
        c = client(on_delivery_failure=listener, logger=logging.getLogger("intempt.listener"))

        assert c.track("a", user_id="u1") is False
        raised = [r for r in caplog.records if "on_delivery_failure raised" in r.getMessage()]
        assert len(raised) == 1 and raised[0].exc_info[0] is RuntimeError

    def test_a_deliberate_discard_is_not_a_failure(self, client, logger):
        heard = Heard()
        c = client(on_delivery_failure=heard)

        assert c._deliver_now(lambda: False, 1, ()) is False
        assert heard.failures == [] and logger.calls["error"] == []

    def test_the_resend_wait_uses_the_real_clock_by_default(self, client):
        import time

        assert client()._sleep is time.sleep

    def test_an_empty_track_batch_is_accepted(self, client, server):
        assert client().track_batch([]) is True
        assert server.requests == []

    def test_a_batched_track_batch_queues_every_event(self, batched):
        c = batched()
        assert c.track_batch([{"event": "a", "user_id": "u1"}, {"event": "b", "user_id": "u1"}])
        assert c.buffered == 2

    def test_events_queued_before_opt_out_are_discarded_not_counted(self, batched, logger):
        c = batched()
        c.track("a", user_id="u1")
        c.track("b", user_id="u1")
        c.opt_out()

        assert c.flush() == FlushResult(sent=0, failed=0)
        assert logger.has("warning", "discarding 2 buffered event(s)")


class TestBufferFailureDetail:
    def test_an_event_refused_by_a_stopped_buffer_is_reported(self):
        heard = Heard()
        buffer = buffer_with(lambda events: None, heard)
        buffer.close()

        assert buffer.enqueue(event("late")) is False
        assert heard.shapes == [("dropped", None, 1, ("late",), None, True)]

    def test_an_eviction_logs_which_event_and_the_cap(self, logger):
        buffer = buffer_with(scripted(), Heard(), size=5, max_queue=5)
        buffer._logger = logger
        for name in "abcde":
            buffer._queue.append(event(name))
        buffer.enqueue(event("new"))

        assert logger.context["error"][0] == {"event_name": "a", "max_queue": 5}

    def test_a_batch_that_cannot_be_encoded_is_dropped_with_a_traceback(self, caplog):
        import logging

        def send(_events):
            raise TypeError("not JSON serialisable")

        heard = Heard()
        caplog.set_level(logging.ERROR, logger="intempt.encode")
        buffer = buffer_with(send, heard)
        buffer._logger = logging.getLogger("intempt.encode")
        buffer.enqueue(event("a"))
        buffer.enqueue(event("b"))

        assert buffer.flush() == FlushResult(sent=0, failed=2)
        assert heard.shapes == [("dropped", None, 2, ("a", "b"), None, True)]
        assert buffer.size == 0
        logged = [r for r in caplog.records if "could not be sent" in r.getMessage()]
        assert len(logged) == 1 and logged[0].exc_info[0] is TypeError

    def test_failed_counts_every_dropped_batch_with_structured_fields(self, logger):
        buffer = buffer_with(scripted(api(400), api(400)), Heard(), size=2)
        buffer._logger = logger
        for name in "abcd":
            buffer._queue.append(event(name))

        assert buffer.flush() == FlushResult(sent=0, failed=4)
        assert logger.context["error"] == [{"status": 400, "kind": "terminal", "count": 2}] * 2

    def test_a_kept_batch_is_forgotten_once_delivered(self, logger):
        down = IntemptApiError("refused", reached=False)
        buffer = buffer_with(scripted(down), Heard(), size=10)
        buffer._logger = logger
        buffer.enqueue(event("a"))
        buffer.enqueue(event("b"))
        buffer.flush()
        assert logger.has("warning", "2 event(s) kept for the next flush")

        assert buffer.flush() == FlushResult(sent=2, failed=0)
        assert buffer._reported_unreached == set()
        buffer.close()

    def test_a_stopped_buffer_sends_nothing_even_with_events_left(self):
        send = scripted()
        buffer = buffer_with(send, Heard())
        buffer._stopped = True
        buffer._queue.append(event("a"))

        assert buffer.flush() == FlushResult(sent=0, failed=0)
        assert send.calls == []

    def test_what_close_abandons_is_reported_and_a_later_flush_is_harmless(self):
        heard = Heard()
        buffer = buffer_with(scripted(), heard, close_budget_s=0.0)
        buffer.enqueue(event("a"))
        buffer.enqueue(event("b"))

        assert buffer.close() == FlushResult(sent=0, failed=2)
        assert heard.shapes == [("dropped", None, 2, ("a", "b"), None, True)]
        assert buffer.flush() == FlushResult(sent=0, failed=0)

    def test_close_does_not_sleep_past_its_budget_on_a_retry_after(self):
        heard = Heard()
        buffer = buffer_with(scripted(api(503, retry_after_ms=2_000)), heard, close_budget_s=1.0)
        buffer.enqueue(event("a"))

        assert buffer.close() == FlushResult(sent=0, failed=1)
        assert buffer._sleep.waits == []
        assert [f.kind for f in heard.failures] == ["dropped"]

    def test_the_exit_hook_is_registered_and_removed_on_close(self, monkeypatch):
        import atexit

        registered: list = []
        removed: list = []
        monkeypatch.setattr(atexit, "register", registered.append)
        monkeypatch.setattr(atexit, "unregister", removed.append)
        buffer = Buffer(
            options=BatchOptions(size=10, flush_ms=60_000, max_queue=100, flush_on_exit=True),
            max_request_events=50,
            logger=_Quiet(),
            send=lambda events: None,
        )
        assert registered == [buffer._on_exit]

        buffer.close()
        assert removed == [buffer._on_exit]
        assert buffer._exit_hook is None

    def test_a_stopped_buffer_schedules_no_timer(self):
        buffer = buffer_with(scripted(), Heard())
        buffer.close()

        buffer._schedule_flush()
        assert buffer._timer is None

    def test_the_timer_is_a_daemon_that_flushes(self):
        import threading

        delivered = threading.Event()
        buffer = Buffer(
            options=BatchOptions(size=10, flush_ms=10, max_queue=100, flush_on_exit=False),
            max_request_events=50,
            logger=_Quiet(),
            send=lambda events: delivered.set(),
        )
        buffer.enqueue(event("a"))
        timer = buffer._timer
        assert timer is not None and timer.daemon is True

        assert delivered.wait(5)
        buffer.close()
