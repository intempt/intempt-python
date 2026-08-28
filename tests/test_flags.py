"""The cross-SDK flag surface, per ``intempt-swift/docs/SDK-API-CONTRACT.md``.

The assertions that matter are the failure ones. A flag SDK is judged on what it returns when the
service is unreachable, not on the happy path.
"""

from __future__ import annotations

import json

import pytest

from intempt import FlagContext, IntemptConfigError
from tests.conftest import Reply

CTX = FlagContext(user_id="u-1", profile_id="p-1")


def _choices(*items: dict) -> Reply:
    return Reply(body=json.dumps({"choices": list(items)}))


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


class TestWaitForInitialization:
    def test_returns_immediately_because_evaluation_is_remote(self, client, server):
        c = client()
        assert c.wait_for_initialization(5000) is None
