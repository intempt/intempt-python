"""The cross-SDK flag surface, per ``intempt-swift/docs/SDK-API-CONTRACT.md``.

The assertions that matter are the failure ones. A flag SDK is judged on what it returns when the
service is unreachable, not on the happy path.
"""

from __future__ import annotations

import json

import pytest

from intempt import FlagContext, IntemptConfigError
from tests.conftest import ORG, PROJECT, SOURCE, Reply

CTX = FlagContext(user_id="u-1", profile_id="p-1")
CHOOSE_PATH = f"/v1/{ORG}/projects/{PROJECT}/optimization/choose-api"


def _choices(*items: dict) -> Reply:
    return Reply(body=json.dumps({"choices": list(items)}))


class TestTheRequest:
    """What actually goes over the wire.

    Every other class here asserts on the RESPONSE, and a response-only suite cannot see the
    request at all: delete ``"device": "all"`` from ``_choose_or_empty`` and each of them still
    passes, while in production ``ExperienceRequest.getDevice()`` turns the missing value into the
    SQL predicate ``"0"`` and every evaluation returns zero experiences. These are the assertions
    that go red when the shape drifts from the other SDKs.
    """

    def test_posts_the_evaluation_path(self, client, server):
        server.expect(_choices({"name": "k", "body": 1}))
        client().variation("k", CTX, 0)

        assert server.requests[-1].method == "POST"
        assert server.requests[-1].path == CHOOSE_PATH

    def test_sends_device_all(self, client, server):
        # Load-bearing, not decoration: a null device is the false predicate "0" server-side, so
        # omitting this key makes EVERY evaluation return nothing.
        server.expect(_choices({"name": "k", "body": 1}))
        client().variation("k", CTX, 0)

        assert server.requests[-1].body["device"] == "all"

    def test_nests_identity_under_identification(self, client, server):
        server.expect(_choices({"name": "k", "body": 1}))
        client().variation("k", CTX, 0)

        assert server.requests[-1].body["identification"] == {
            "userId": "u-1",
            "profileId": "p-1",
            "sourceId": SOURCE,
        }

    def test_keeps_the_source_id_a_string(self, client, server):
        # A 19-digit snowflake past 2**53 loses precision the moment anything treats it as a
        # number, and a wrong source id is a wrong audience rather than an error.
        server.expect(_choices({"name": "k", "body": 1}))
        client().variation("k", CTX, 0)

        assert server.requests[-1].body["identification"]["sourceId"] == SOURCE
        assert isinstance(server.requests[-1].body["identification"]["sourceId"], str)

    def test_asks_for_exactly_the_requested_key(self, client, server):
        server.expect(_choices({"name": "k", "body": 1}))
        client().variation("k", CTX, 0)

        assert server.requests[-1].body["names"] == ["k"]

    def test_all_flags_omits_names_entirely(self, client, server):
        # `ExperienceUtils.selector` sets includeAll only when names AND groups are both empty or
        # null. Sending `names: []` is not the same request as sending no names.
        server.expect(_choices())
        client().all_flags(CTX)

        assert "names" not in server.requests[-1].body

    def test_sends_the_session_id_when_one_is_given(self, client, server):
        server.expect(_choices({"name": "k", "body": 1}))
        client().variation("k", FlagContext(user_id="u-1", session_id="s-9"), 0)

        assert server.requests[-1].body["sessionId"] == "s-9"

    def test_omits_the_session_id_rather_than_sending_null(self, client, server):
        # An absent field is omitted, never sent as null -- a present key is an assertion.
        server.expect(_choices({"name": "k", "body": 1}))
        client().variation("k", FlagContext(user_id="u-1"), 0)

        assert "sessionId" not in server.requests[-1].body

    def test_omits_an_absent_profile_id(self, client, server):
        server.expect(_choices({"name": "k", "body": 1}))
        client().variation("k", FlagContext(user_id="u-1"), 0)

        assert "profileId" not in server.requests[-1].body["identification"]


class TestIdentity:
    """A context the service structurally cannot answer.

    ``buildAudienceRequest`` raises unless it can take the PROFILE branch (source id + non-blank
    profile id) or the USER branch (user id). That reaches the SDK as a 5xx, which the flag path
    absorbs into the caller's default -- so without this check a misconfigured client serves
    defaults forever and looks like a working integration.
    """

    def test_refuses_a_profile_only_context_when_no_source_id_is_configured(self, client, server):
        c = client(source_id=None)
        with pytest.raises(IntemptConfigError, match="user_id"):
            c.variation("k", FlagContext(profile_id="p-1"), "x")

        assert server.requests == []

    def test_refuses_an_empty_context(self, client, server):
        c = client()
        with pytest.raises(IntemptConfigError, match="user_id"):
            c.variation("k", FlagContext(), "x")

        assert server.requests == []

    def test_refuses_a_blank_user_id(self, client, server):
        c = client(source_id=None)
        with pytest.raises(IntemptConfigError, match="user_id"):
            c.variation("k", FlagContext(user_id="   "), "x")

        assert server.requests == []

    def test_accepts_a_profile_when_a_source_id_is_configured(self, client, server):
        server.expect(_choices({"name": "k", "body": "served"}))
        c = client()

        assert c.variation("k", FlagContext(profile_id="p-1"), "x") == "served"

    def test_all_flags_refuses_the_same_context(self, client, server):
        c = client(source_id=None)
        with pytest.raises(IntemptConfigError, match="user_id"):
            c.all_flags(FlagContext(profile_id="p-1"))

        assert server.requests == []

    @pytest.mark.parametrize("bad", [None, {"user_id": "u-1"}, "u-1"])
    def test_refuses_something_that_is_not_a_flag_context(self, client, server, bad):
        # A mapping is the plausible mistake, and without this it reaches `context.user_id` and
        # dies as an AttributeError from inside the SDK rather than as a named config error.
        c = client()
        with pytest.raises(IntemptConfigError, match="FlagContext"):
            c.variation("k", bad, "x")

        assert server.requests == []

    def test_the_context_carries_no_account_id(self):
        # `Identification` on the service has sourceId/profileId/userId and nothing else, so an
        # account id had nowhere to go and was accepted in silence.
        assert not hasattr(FlagContext(), "account_id")


class TestKeyCharset:
    """The service validates every name against ``^[a-zA-Z0-9_-]*$`` and 400s otherwise.

    A 400 is absorbed into the caller's default exactly like an unknown key, so a dotted or
    spaced key is a flag that is dead in production with one warning line to show for it.
    """

    @pytest.mark.parametrize("key", ["pricing.cta", "checkout v2", "feature:new", "a/b"])
    def test_refuses_a_key_the_service_would_reject(self, client, server, key):
        c = client()
        with pytest.raises(IntemptConfigError, match="key"):
            c.variation(key, CTX, "x")

        assert server.requests == []

    @pytest.mark.parametrize("key", ["checkout_v2", "pricing-cta", "AB123", "a"])
    def test_accepts_a_key_the_service_would_accept(self, client, server, key):
        server.expect(_choices({"name": key, "body": "served"}))
        c = client()

        assert c.variation(key, CTX, "x") == "served"


class TestVariation:
    def test_returns_the_served_value(self, client, server):
        # `variation`, not `variation_detail` -- the detail method is internal until the platform
        # sends a reason. Note this fixture supplies "group" and "reason" and the serving response
        # carries NEITHER today, which is exactly why asserting on them proved nothing.
        server.expect(
            _choices({"name": "checkout_v2", "group": "B", "body": True, "reason": "targeted"})
        )
        c = client()

        assert c.variation("checkout_v2", CTX, False) is True

    def test_returns_the_default_when_the_served_body_is_null(self, client, server):
        # NOT the holdout case, which cannot be asserted: a held-back person's experience is
        # absent from the response entirely rather than present with a cause. Telling a holdout
        # from an outage needs a reason the platform does not send.
        server.expect(_choices({"name": "checkout_v2", "body": None}))
        c = client()

        assert c.variation("checkout_v2", CTX, "fallback") == "fallback"

    def test_returns_the_default_when_the_service_is_unreachable(self, client, server):
        server.expect(Reply(status=500))
        c = client()

        assert c.variation("checkout_v2", CTX, "safe") == "safe"

    def test_returns_the_default_when_the_response_is_not_an_object(self, client, server):
        # A proxy or an error page can answer 200 with something that is not the envelope. That
        # is an unanswered request, not an answer of `[]`.
        server.expect(Reply(body=json.dumps([{"name": "checkout_v2", "body": True}])))
        c = client()

        assert c.variation("checkout_v2", CTX, "safe") == "safe"

    def test_returns_the_default_when_choices_is_not_a_list(self, client, server):
        server.expect(Reply(body=json.dumps({"choices": {"checkout_v2": True}})))
        c = client()

        assert c.variation("checkout_v2", CTX, "safe") == "safe"

    def test_returns_the_default_when_the_key_is_unknown(self, client, server):
        server.expect(_choices())
        c = client()

        assert c.variation("never_created", CTX, "safe") == "safe"

    def test_refuses_an_empty_key(self, client, server):
        c = client()
        with pytest.raises(IntemptConfigError, match="key"):
            c.variation("   ", CTX, "x")


class TestTypedHelpers:
    def test_falls_back_rather_than_coercing(self, client, server):
        # bool("false") is True. A silent coercion is indistinguishable from a correct answer,
        # which is worse than returning the default the caller chose.
        server.expect(_choices({"name": "f", "body": "false", "reason": "targeted"}))
        c = client()

        assert c.bool_variation("f", CTX, False) is False

    def test_a_boolean_is_not_a_number(self, client, server):
        # bool subclasses int in Python, so True would otherwise pass as 1.
        server.expect(_choices({"name": "f", "body": True, "reason": "targeted"}))
        c = client()

        assert c.number_variation("f", CTX, 0) == 0

    def test_accepts_a_correctly_typed_value(self, client, server):
        server.expect(_choices({"name": "f", "body": 42, "reason": "targeted"}))
        c = client()

        assert c.number_variation("f", CTX, 0) == 42


class TestAllFlags:
    def test_returns_every_key_in_one_call(self, client, server):
        server.expect(
            _choices(
                {"name": "a", "body": 1, "reason": "targeted"},
                {"name": "b", "body": 2, "reason": "targeted"},
            )
        )
        c = client()

        assert c.all_flags(CTX) == {"a": 1, "b": 2}

    def test_omits_a_key_with_no_body_rather_than_mapping_it_to_none(self, client, server):
        # The identical wire response makes `variation` return the caller's default, so a None
        # here would have the two public read paths disagree -- and would read as "assigned
        # nothing" when it means "not assigned".
        server.expect(_choices({"name": "a", "body": 1}, {"name": "b", "body": None}))
        c = client()

        assert c.all_flags(CTX) == {"a": 1}

    def test_keeps_a_falsy_body(self, client, server):
        # False and 0 are values a caller chose. Only a null is an absent answer.
        server.expect(_choices({"name": "a", "body": False}, {"name": "b", "body": 0}))
        c = client()

        assert c.all_flags(CTX) == {"a": False, "b": 0}


class TestWaitForInitialization:
    def test_returns_immediately_because_evaluation_is_remote(self, client, server):
        c = client()
        assert c.wait_for_initialization(5000) is None

    def test_the_timeout_is_accepted_and_ignored(self, client, server):
        # The argument exists so a port from an SDK that polls a local flag store still compiles.
        # It is inert, and "inert" needs an assertion: a caller who passes a minute must not wait
        # one, and must not be able to tell 60_000 from 0 either.
        import time

        c = client()
        started = time.monotonic()
        assert c.wait_for_initialization(60_000) is None
        assert c.wait_for_initialization(0) is None
        assert c.wait_for_initialization() is None
        assert time.monotonic() - started < 1.0

    def test_it_still_refuses_a_closed_client(self, client, server):
        c = client()
        c.close()
        with pytest.raises(IntemptConfigError, match="client is closed"):
            c.wait_for_initialization(1)
