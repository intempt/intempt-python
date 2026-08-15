"""Transport behaviour over a real socket: framing, reuse, timeouts, failures."""

from __future__ import annotations

import json
import threading

import pytest

from intempt import IntemptApiError, IntemptConfigError
from intempt._config import resolve_config
from intempt._transport import ApiKeyCredentials, Transport
from tests.conftest import API_KEY, ORG, PROJECT, SOURCE, Reply

TRACK_PATH = f"/v1/{ORG}/projects/{PROJECT}/sources/{SOURCE}/track"


class TestFraming:
    def test_never_puts_the_key_in_the_request_line(self, client, server):
        client().track("purchase", user_id="u1")
        assert "apiKey" not in server.requests[0].path
        assert "?" not in server.requests[0].path

    def test_the_credential_decodes_the_way_a_server_would(self, client, server):
        import base64

        client().track("purchase", user_id="u1")
        scheme, _, value = server.requests[0].headers["authorization"].partition(" ")
        assert scheme == "Basic"
        prefix, _, secret = base64.b64decode(value).decode().partition(":")
        assert f"{prefix}.{secret}" == API_KEY

    def test_sends_a_library_header(self, client, server):
        client().track("purchase", user_id="u1")
        assert server.requests[0].headers["x-intempt-lib"].startswith("intempt-python/")


class TestConnectionReuse:
    def test_reuses_one_connection_across_calls(self, client, server):
        c = client()
        for name in ("a", "b", "c"):
            c.track(name, user_id="u1")

        assert len(server.requests) == 3
        assert len({r.socket_id for r in server.requests}) == 1

    def test_opens_a_fresh_connection_per_call_when_keep_alive_is_off(self, client, server):
        c = client(keep_alive=False)
        c.track("a", user_id="u1")
        c.track("b", user_id="u1")
        assert len({r.socket_id for r in server.requests}) == 2

    def test_changing_host_drops_the_pooled_connection(self, client, server):
        c = client()
        c.track("a", user_id="u1")
        first = server.requests[0].socket_id
        # Point at the same server by a different spelling of the host so the
        # config compares unequal and the pool must be rebuilt.
        c.set_config(host=server.host.replace("127.0.0.1", "localhost"))
        c.track("b", user_id="u1")
        assert server.requests[1].socket_id != first


class TestFailures:
    def test_times_out_on_a_slow_server(self, client, server):
        server.expect(Reply(delay_s=0.5))
        c = client(timeout=0.05)
        with pytest.raises(IntemptApiError, match="timed out"):
            c.track("slow", user_id="u1")

    def test_a_timeout_is_retryable(self, client, server):
        server.expect(Reply(delay_s=0.5))
        c = client(timeout=0.05)
        try:
            c.track("slow", user_id="u1")
        except IntemptApiError as exc:
            assert exc.status is None
            assert exc.retryable is True

    def test_a_200_with_a_non_json_body_does_not_crash(self, client, server):
        server.expect(Reply(body="<html>gateway</html>"))
        client().track("purchase", user_id="u1")

    def test_an_empty_body_returns_none(self, client, server):
        server.expect(Reply(body=""))
        assert client().recommend(user_id="u1", feed_id="f", fields=["id"]) is None

    def test_carries_retry_after_onto_the_error(self, client, server):
        server.expect(Reply(status=429, headers={"Retry-After": "2"}))
        with pytest.raises(IntemptApiError) as caught:
            client().track("purchase", user_id="u1")
        assert caught.value.retry_after_ms == 2000

    def test_leaves_retry_after_unset_when_absent(self, client, server):
        server.expect(Reply(status=500, body="boom"))
        with pytest.raises(IntemptApiError) as caught:
            client().track("purchase", user_id="u1")
        assert caught.value.retry_after_ms is None

    def test_a_dead_pooled_connection_is_replaced(self, client, server):
        """The server closing a kept-alive socket must not fail the next call."""
        c = client()
        c.track("a", user_id="u1")
        # Close the pooled connection underneath the client.
        c._transport._conn.close()
        c.track("b", user_id="u1")
        assert len(server.requests) == 2

    def test_posting_after_close_raises(self, client):
        c = client()
        c.close()
        with pytest.raises(IntemptConfigError, match="client is closed"):
            c.track("a", user_id="u1")


class TestSerialisation:
    def test_serialises_a_datetime_in_properties(self, client, server):
        import datetime as _dt

        when = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
        client().track("purchase", user_id="u1", properties={"at": when})
        assert server.requests[0].body["track"][0]["payload"][0]["data"]["at"] == int(
            when.timestamp() * 1000
        )

    def test_serialises_a_set_as_a_list(self, client, server):
        client().track("purchase", user_id="u1", properties={"tags": {"a"}})
        assert server.requests[0].body["track"][0]["payload"][0]["data"]["tags"] == ["a"]

    def test_rejects_an_unserialisable_value(self, client):
        with pytest.raises(TypeError, match="not JSON serialisable"):
            client().track("purchase", user_id="u1", properties={"fn": object()})

    def test_does_not_pollute_object_prototype_equivalent(self, client, server):
        """A __proto__-style key is just data in Python, and must travel as data."""
        polluted = json.loads('{"__proto__": {"pwned": true}}')
        client().track("purchase", user_id="u1", properties=polluted)
        data = server.requests[0].body["track"][0]["payload"][0]["data"]
        assert data == {"__proto__": {"pwned": True}}


class TestConcurrency:
    def test_track_batch_runs_bounded_parallel_requests(self, client, server):
        server.expect(*[Reply(delay_s=0.02) for _ in range(6)])
        c = client(max_request_events=1, max_concurrent_requests=3)
        c.track_batch([{"event": f"e{i}", "user_id": "u1"} for i in range(6)])

        assert len(server.requests) == 6
        # Three workers on a shared cursor means at most three sockets.
        assert len({r.socket_id for r in server.requests}) <= 3

    def test_a_failure_in_one_chunk_surfaces(self, client, server):
        server.expect(Reply(), Reply(status=500, body="boom"), Reply())
        c = client(max_request_events=1, max_concurrent_requests=3)
        with pytest.raises(IntemptApiError):
            c.track_batch([{"event": f"e{i}", "user_id": "u1"} for i in range(3)])

    def test_one_client_is_safe_across_threads(self, client, server):
        server.expect(*[Reply() for _ in range(20)])
        c = client()
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                c.track(f"e{index}", user_id="u1")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert len(server.requests) == 20


class TestTransportUnit:
    def test_set_config_without_a_host_change_keeps_the_connection(self, server):
        config = resolve_config(
            org=ORG, project=PROJECT, api_key=API_KEY, host=server.host, scheme="http"
        )
        transport = Transport(config, ApiKeyCredentials(API_KEY))
        transport.post(config.project_path("/track"), {"track": []})
        first = transport._conn
        transport.set_config(config)
        assert transport._conn is first
        transport.close()

    def test_close_is_idempotent(self, server):
        config = resolve_config(
            org=ORG, project=PROJECT, api_key=API_KEY, host=server.host, scheme="http"
        )
        transport = Transport(config, ApiKeyCredentials(API_KEY))
        transport.close()
        transport.close()
