"""The public method surface, over a real socket."""

from __future__ import annotations

import base64
import datetime as _dt

import pytest

from intempt import IntemptApiError, IntemptConfigError
from tests.conftest import API_KEY, ORG, PROJECT, SOURCE, Reply

TRACK_PATH = f"/v1/{ORG}/projects/{PROJECT}/sources/{SOURCE}/track"
CONSENT_PATH = f"/v1/{ORG}/projects/{PROJECT}/consents/data"


class TestTrack:
    def test_sends_a_well_formed_post(self, client, server):
        c = client()
        c.track("purchase", user_id="u1", properties={"total": 99.99})

        assert len(server.requests) == 1
        req = server.requests[0]
        assert req.method == "POST"
        assert req.path == TRACK_PATH
        assert req.headers["content-type"] == "application/json"
        assert int(req.headers["content-length"]) > 0

        expected = base64.b64encode(b"pfx0123456789abcdef:sec0123456789abcdef").decode()
        assert req.headers["authorization"] == f"Basic {expected}"
        assert req.headers["x-intempt-lib"].startswith("intempt-python/")

        item = req.body["track"][0]
        assert item["name"] == "purchase"
        assert item["payload"][0]["userId"] == "u1"
        assert item["payload"][0]["data"] == {"total": 99.99}
        assert item["payload"][0]["eventId"]

    def test_uses_the_sourceless_path_without_a_source_id(self, client, server):
        c = client(source_id=None)
        c.track("purchase", user_id="u1")
        assert server.requests[0].path == f"/v1/{ORG}/projects/{PROJECT}/track"

    def test_requires_an_identifier(self, client):
        with pytest.raises(IntemptConfigError, match="one of user_id or account_id"):
            client().track("purchase")

    def test_treats_a_whitespace_identifier_as_missing(self, client):
        with pytest.raises(IntemptConfigError, match="one of user_id or account_id"):
            client().track("purchase", user_id="   ")

    def test_requires_an_event_name(self, client):
        with pytest.raises(IntemptConfigError, match="event name is required"):
            client().track("", user_id="u1")

    def test_refuses_the_reserved_identify_name(self, client):
        with pytest.raises(IntemptConfigError, match="reserved"):
            client().track("Identify", user_id="u1")

    def test_accepts_an_account_id_alone(self, client, server):
        client().track("purchase", account_id="acme")
        assert server.requests[0].body["track"][0]["payload"][0]["accountId"] == "acme"


class TestTrackBatch:
    def test_sends_many_events_in_one_request(self, client, server):
        client().track_batch(
            [
                {"event": "a", "user_id": "u1"},
                {"event": "b", "user_id": "u1"},
            ]
        )
        assert len(server.requests) == 1
        assert len(server.requests[0].body["track"]) == 2

    def test_chunks_at_max_request_events(self, client, server):
        server.expect(Reply(), Reply(), Reply())
        client(max_request_events=2).track_batch(
            [{"event": f"e{i}", "user_id": "u1"} for i in range(5)]
        )
        widths = [len(r.body["track"]) for r in server.requests]
        assert sum(widths) == 5
        assert max(widths) == 2

    def test_names_the_offending_index(self, client):
        with pytest.raises(IntemptConfigError, match=r"track_batch\[1\]"):
            client().track_batch([{"event": "ok", "user_id": "u1"}, {"event": "", "user_id": "u1"}])

    def test_empty_sequence_is_a_no_op(self, client, server):
        client().track_batch([])
        assert server.requests == []

    def test_rejects_a_non_sequence(self, client):
        with pytest.raises(IntemptConfigError, match="must be a sequence"):
            client().track_batch(object())  # type: ignore[arg-type]


class TestIdentity:
    def test_identify_uses_the_reserved_event(self, client, server):
        client().identify(user_id="u1", traits={"plan": "pro"})
        item = server.requests[0].body["track"][0]
        assert item["name"] == "Identify"
        assert item["payload"][0]["userAttributes"] == {"plan": "pro"}

    def test_identify_accepts_an_event_override(self, client, server):
        client().identify(user_id="u1", event="Signed up")
        assert server.requests[0].body["track"][0]["name"] == "Signed up"

    def test_group_requires_an_account_id(self, client):
        with pytest.raises(IntemptConfigError, match="account_id must be a non-empty string"):
            client().group(account_id=" ", user_id="u1")

    def test_group_sends_account_attributes(self, client, server):
        client().group(user_id="u1", account_id="acme", attributes={"tier": "ent"})
        payload = server.requests[0].body["track"][0]["payload"][0]
        assert payload["accountId"] == "acme"
        assert payload["accountAttributes"] == {"tier": "ent"}

    def test_alias_carries_both_identities(self, client, server):
        client().alias(user_id="new", previous_user_id="old")
        payload = server.requests[0].body["track"][0]["payload"][0]
        assert payload["userId"] == "new"
        assert payload["anotherUserId"] == "old"

    @pytest.mark.parametrize(
        "kwargs,field",
        [
            ({"user_id": " ", "previous_user_id": "old"}, "user_id"),
            ({"user_id": "new", "previous_user_id": " "}, "previous_user_id"),
        ],
    )
    def test_alias_names_the_blank_field(self, client, kwargs, field):
        with pytest.raises(IntemptConfigError, match=f"{field} must be a non-empty string"):
            client().alias(**kwargs)


class TestEcommerce:
    def test_product_viewed(self, client, server):
        client().ecommerce.product_viewed(product_id="sku-1", user_id="u1")
        item = server.requests[0].body["track"][0]
        assert item["name"] == "Product viewed"
        assert item["payload"][0]["data"] == {"productId": "sku-1"}

    def test_added_to_cart_requires_a_positive_quantity(self, client):
        with pytest.raises(IntemptConfigError, match="quantity must be a positive integer"):
            client().ecommerce.added_to_cart(product_id="sku-1", quantity=0, user_id="u1")

    def test_ordered_sends_one_line_per_product(self, client, server):
        client().ecommerce.ordered(
            user_id="u1",
            products=[{"product_id": "a", "quantity": 2}, {"product_id": "b"}],
        )
        payload = server.requests[0].body["track"][0]["payload"]
        assert len(payload) == 2
        assert payload[0]["data"] == {"productId": "a", "quantity": 2}
        assert payload[1]["data"] == {"productId": "b"}

    def test_ordered_lines_share_one_event_id(self, client, server):
        """Bit-compatible with 1.x. Safe because nothing dedupes on eventId."""
        client().ecommerce.ordered(
            user_id="u1", products=[{"product_id": "a"}, {"product_id": "b"}]
        )
        payload = server.requests[0].body["track"][0]["payload"]
        assert payload[0]["eventId"] == payload[1]["eventId"]

    def test_ordered_rejects_an_empty_product_list(self, client):
        with pytest.raises(IntemptConfigError, match="non-empty sequence"):
            client().ecommerce.ordered(user_id="u1", products=[])

    def test_ordered_names_the_offending_index(self, client):
        with pytest.raises(IntemptConfigError, match=r"products\[1\]"):
            client().ecommerce.ordered(
                user_id="u1", products=[{"product_id": "a"}, {"quantity": 1}]
            )


class TestConsent:
    def test_grant_sends_epoch_seconds_not_milliseconds(self, client, server):
        """The consent endpoint compares timestamp * 1000 against ms bounds."""
        when = _dt.datetime(2026, 8, 15, 12, 0, tzinfo=_dt.timezone.utc)
        client().consent.grant(user_id="u1", category="marketing", timestamp=when)

        body = server.requests[0].body
        assert server.requests[0].path == CONSENT_PATH
        assert body["action"] == "accept"
        assert body["timestamp"] == int(when.timestamp())
        # Milliseconds would put this past 2040, where the server silently
        # replaces it with its own clock.
        assert body["timestamp"] * 1000 < 2_216_872_268_000

    def test_revoke_sends_reject(self, client, server):
        client().consent.revoke(user_id="u1")
        assert server.requests[0].body["action"] == "reject"

    def test_defaults_valid_until_to_unlimited(self, client, server):
        client().consent.grant(user_id="u1")
        assert server.requests[0].body["validUntil"] == "unlimited"

    def test_requires_an_identifier(self, client):
        with pytest.raises(IntemptConfigError, match="user_id must be a non-empty string"):
            client().consent.grant(category="marketing")

    def test_profile_id_requires_a_source_id(self, client):
        with pytest.raises(IntemptConfigError, match="source_id must be configured"):
            client(source_id=None).consent.grant(profile_id="p1")

    def test_source_id_is_sent_as_a_string(self, client, server):
        """Number() would round the last digits and address another source."""
        client().consent.grant(profile_id="p1")
        assert server.requests[0].body["sourceId"] == SOURCE
        assert isinstance(server.requests[0].body["sourceId"], str)


class TestRecommend:
    def test_resolves_a_user_entity(self, client, server):
        server.expect(Reply(body='{"items":[{"id":"1"}]}'))
        result = client().recommend(user_id="u1", feed_id="5292", fields=["id"])

        assert server.requests[0].path == f"/v1/{ORG}/projects/{PROJECT}/feeds/5292/data"
        assert server.requests[0].body["id"] == "u1"
        assert server.requests[0].body["type"] == "user"
        assert result == {"items": [{"id": "1"}]}

    def test_resolves_an_account_entity(self, client, server):
        client().recommend(account_id="acme", feed_id="f", fields=["id"])
        assert server.requests[0].body["type"] == "account"

    def test_refuses_both_identifiers(self, client):
        with pytest.raises(IntemptConfigError, match="not both"):
            client().recommend(user_id="u1", account_id="acme", feed_id="f", fields=["id"])

    def test_requires_one_identifier(self, client):
        with pytest.raises(IntemptConfigError, match="one of user_id or account_id"):
            client().recommend(feed_id="f", fields=["id"])

    def test_requires_non_empty_fields(self, client):
        with pytest.raises(IntemptConfigError, match="fields must be a non-empty sequence"):
            client().recommend(user_id="u1", feed_id="f", fields=[])

    def test_rejects_a_non_positive_limit(self, client):
        with pytest.raises(IntemptConfigError, match="limit must be a positive integer"):
            client().recommend(user_id="u1", feed_id="f", fields=["id"], limit=0)


class TestPrivacy:
    def test_opt_out_suppresses_every_write(self, client, server):
        c = client()
        c.opt_out()
        c.track("purchase", user_id="u1")
        c.identify(user_id="u1")
        c.consent.grant(user_id="u1")
        c.ecommerce.ordered(user_id="u1", products=[{"product_id": "a"}])
        assert server.requests == []

    def test_opt_in_restores_sending(self, client, server):
        c = client()
        c.opt_out()
        c.opt_in()
        c.track("purchase", user_id="u1")
        assert len(server.requests) == 1

    def test_recommend_is_unaffected_by_opt_out(self, client, server):
        c = client()
        c.opt_out()
        c.recommend(user_id="u1", feed_id="f", fields=["id"])
        assert len(server.requests) == 1

    def test_is_opted_in_reports_false_after_close(self, client):
        c = client()
        assert c.is_opted_in() is True
        c.close()
        assert c.is_opted_in() is False


class TestLifecycle:
    def test_calls_after_close_raise(self, client):
        c = client()
        c.close()
        with pytest.raises(IntemptConfigError, match="client is closed"):
            c.track("purchase", user_id="u1")

    def test_recommend_is_gated_after_close(self, client):
        c = client()
        c.close()
        with pytest.raises(IntemptConfigError, match="client is closed"):
            c.recommend(user_id="u1", feed_id="f", fields=["id"])

    def test_close_is_idempotent(self, client):
        c = client()
        c.close()
        c.close()

    def test_works_as_a_context_manager(self, client, server):
        with client() as c:
            c.track("purchase", user_id="u1")
        assert len(server.requests) == 1


class TestErrors:
    def test_surfaces_a_500_with_status_and_body(self, client, server):
        server.expect(Reply(status=500, body='{"error":"boom"}'))
        with pytest.raises(IntemptApiError) as caught:
            client().track("purchase", user_id="u1")
        assert caught.value.status == 500
        assert "boom" in caught.value.body
        assert caught.value.retryable is True

    def test_a_400_is_not_retryable(self, client, server):
        server.expect(Reply(status=400, body="bad"))
        with pytest.raises(IntemptApiError) as caught:
            client().track("purchase", user_id="u1")
        assert caught.value.retryable is False

    def test_never_leaks_the_credential(self, client, server):
        """The key must not reach any error surface a log might capture."""
        import traceback

        server.expect(Reply(status=500, body="boom"))
        try:
            client().track("purchase", user_id="u1")
        except IntemptApiError as exc:
            views = [
                str(exc),
                repr(exc),
                "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            ]
        secret = API_KEY.split(".")[1]
        basic = base64.b64encode(API_KEY.replace(".", ":").encode()).decode()
        for view in views:
            assert secret not in view
            assert basic not in view
