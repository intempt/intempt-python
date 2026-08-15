"""Every rejection message, asserted in full.

The message is the contract. It is the only thing a caller sees when a call is
refused, the docs quote it verbatim (`track_batch[1]: event name is required`),
and it is what someone pastes into a search box at two in the morning. A test
that only checks `pytest.raises(IntemptConfigError)` leaves the text free to say
anything at all.

Mutation testing made the gap concrete: every method label in `_client` — the
`"track"` in `non_blank(event, "track", ...)` — could be replaced with garbage or
case-flipped and the whole suite stayed green, because nothing read the string
those labels build.

Copyright 2026 Intempt Technologies
Licensed under the Apache License, Version 2.0.
"""

from __future__ import annotations

import datetime as _dt
import math

import pytest

from intempt import BatchOptions, Intempt, IntemptConfigError
from intempt._util import chunk, ensure_timestamp, non_blank, require_identifier
from tests.conftest import API_KEY, ORG, PROJECT, SOURCE

BLANKS = [None, "", "   ", 0, [], {}]


def message(excinfo: pytest.ExceptionInfo[IntemptConfigError]) -> str:
    return str(excinfo.value)


@pytest.fixture
def client(server):
    server.reset()
    instance = Intempt(
        org=ORG,
        project=PROJECT,
        api_key=API_KEY,
        source_id=SOURCE,
        host=server.host,
        scheme="http",
    )
    yield instance
    instance.close()


class TestHelpersSpellTheMethodAndField:
    @pytest.mark.parametrize("method", ["track", "group", "alias", "recommend"])
    @pytest.mark.parametrize("field", ["user_id", "product_id", "feed_id"])
    def test_non_blank_names_both_the_method_and_the_field(self, method, field):
        with pytest.raises(IntemptConfigError) as excinfo:
            non_blank("", method, field)

        assert message(excinfo) == f"{method}: {field} must be a non-empty string"

    @pytest.mark.parametrize("value", BLANKS)
    def test_non_blank_rejects_every_empty_shape_with_the_same_message(self, value):
        with pytest.raises(IntemptConfigError) as excinfo:
            non_blank(value, "track", "user_id")

        assert message(excinfo) == "track: user_id must be a non-empty string"

    def test_non_blank_returns_the_value_when_it_is_usable(self):
        assert non_blank(" u1 ", "track", "user_id") == " u1 "

    @pytest.mark.parametrize("method", ["track", "identify", "ordered", "track_batch[3]"])
    def test_require_identifier_names_both_accepted_fields(self, method):
        with pytest.raises(IntemptConfigError) as excinfo:
            require_identifier({}, method)

        assert message(excinfo) == f"{method}: one of user_id or account_id is required"

    @pytest.mark.parametrize("key", ["user_id", "account_id"])
    def test_require_identifier_accepts_either_field_alone(self, key):
        require_identifier({key: "value"}, "track")

    def test_chunk_says_what_the_minimum_size_is(self):
        with pytest.raises(IntemptConfigError) as excinfo:
            chunk([1, 2, 3], 0)

        assert message(excinfo) == "chunk size must be at least 1"

    def test_a_non_finite_timestamp_says_so(self):
        for value in (math.nan, math.inf, -math.inf):
            with pytest.raises(IntemptConfigError) as excinfo:
                ensure_timestamp(value)

            assert message(excinfo) == "timestamp must be a finite number of milliseconds"

    def test_a_datetime_survives_the_round_trip(self):
        moment = _dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=_dt.timezone.utc)

        assert ensure_timestamp(moment) == int(moment.timestamp() * 1000)


class TestCaptureMessages:
    @pytest.mark.parametrize("event", BLANKS)
    def test_track_without_an_event_name_says_track(self, client, event):
        with pytest.raises(IntemptConfigError) as excinfo:
            client.track(event, user_id="u1")

        assert message(excinfo) == "track: event name is required"

    def test_track_without_an_identifier_says_track(self, client):
        with pytest.raises(IntemptConfigError) as excinfo:
            client.track("purchase")

        assert message(excinfo) == "track: one of user_id or account_id is required"

    def test_track_batch_rejects_a_non_sequence(self, client):
        with pytest.raises(IntemptConfigError) as excinfo:
            client.track_batch(42)

        assert message(excinfo) == "track_batch: events must be a sequence"

    @pytest.mark.parametrize("index", [0, 1, 4])
    def test_track_batch_names_the_offending_index(self, client, index):
        events = [{"event": "ok", "user_id": "u1"} for _ in range(index + 1)]
        events[index] = {"user_id": "u1"}

        with pytest.raises(IntemptConfigError) as excinfo:
            client.track_batch(events)

        assert message(excinfo) == f"track_batch[{index}]: event name is required"

    def test_track_batch_names_the_index_for_a_missing_identifier(self, client):
        with pytest.raises(IntemptConfigError) as excinfo:
            client.track_batch([{"event": "ok", "user_id": "u1"}, {"event": "ok"}])

        assert message(excinfo) == "track_batch[1]: one of user_id or account_id is required"

    def test_track_batch_names_the_index_for_a_non_mapping(self, client):
        with pytest.raises(IntemptConfigError) as excinfo:
            client.track_batch([{"event": "ok", "user_id": "u1"}, "nope"])

        assert message(excinfo) == "track_batch[1]: each event must be a mapping"


class TestIdentityMessages:
    def test_identify_without_an_identifier_says_identify(self, client):
        with pytest.raises(IntemptConfigError) as excinfo:
            client.identify()

        assert message(excinfo) == "identify: one of user_id or account_id is required"

    @pytest.mark.parametrize("value", BLANKS)
    def test_group_needs_an_account_id(self, client, value):
        with pytest.raises(IntemptConfigError) as excinfo:
            client.group(user_id="u1", account_id=value)

        assert message(excinfo) == "group: account_id must be a non-empty string"

    @pytest.mark.parametrize(
        ("kwargs", "field"),
        [
            ({"user_id": "", "previous_user_id": "old"}, "user_id"),
            ({"user_id": "new", "previous_user_id": ""}, "previous_user_id"),
        ],
    )
    def test_alias_names_whichever_side_is_missing(self, client, kwargs, field):
        with pytest.raises(IntemptConfigError) as excinfo:
            client.alias(**kwargs)

        assert message(excinfo) == f"alias: {field} must be a non-empty string"


class TestCommerceMessages:
    @pytest.mark.parametrize(
        ("call", "label"),
        [("product_viewed", "product_viewed"), ("added_to_cart", "added_to_cart")],
    )
    def test_a_commerce_call_names_itself_when_the_product_is_missing(self, client, call, label):
        with pytest.raises(IntemptConfigError) as excinfo:
            kwargs = {"quantity": 1} if call == "added_to_cart" else {}
            getattr(client.ecommerce, call)(user_id="u1", product_id="", **kwargs)

        assert message(excinfo) == f"{label}: product_id must be a non-empty string"

    @pytest.mark.parametrize(
        ("call", "label"),
        [("product_viewed", "product_viewed"), ("added_to_cart", "added_to_cart")],
    )
    def test_a_commerce_call_names_itself_when_the_identifier_is_missing(self, client, call, label):
        with pytest.raises(IntemptConfigError) as excinfo:
            kwargs = {"quantity": 1} if call == "added_to_cart" else {}
            getattr(client.ecommerce, call)(product_id="sku-1", **kwargs)

        assert message(excinfo) == f"{label}: one of user_id or account_id is required"

    @pytest.mark.parametrize("quantity", [0, -1, 1.5, "2", None])
    def test_added_to_cart_says_what_a_quantity_must_be(self, client, quantity):
        with pytest.raises(IntemptConfigError) as excinfo:
            client.ecommerce.added_to_cart(user_id="u1", product_id="sku-1", quantity=quantity)

        assert message(excinfo) == "added_to_cart: quantity must be a positive integer"

    @pytest.mark.parametrize("products", [[], (), None, "sku-1", 5])
    def test_ordered_needs_a_non_empty_sequence(self, client, products):
        with pytest.raises(IntemptConfigError) as excinfo:
            client.ecommerce.ordered(user_id="u1", products=products)

        assert message(excinfo) == "ordered: products must be a non-empty sequence"

    @pytest.mark.parametrize("index", [0, 1, 2])
    def test_ordered_names_the_offending_product_index(self, client, index):
        products = [{"product_id": f"sku-{i}"} for i in range(index + 1)]
        products[index] = {"product_id": ""}

        with pytest.raises(IntemptConfigError) as excinfo:
            client.ecommerce.ordered(user_id="u1", products=products)

        assert (
            message(excinfo) == f"ordered: products[{index}]: product_id must be a non-empty string"
        )

    def test_ordered_needs_an_identifier(self, client):
        with pytest.raises(IntemptConfigError) as excinfo:
            client.ecommerce.ordered(products=[{"product_id": "sku-1"}])

        assert message(excinfo) == "ordered: one of user_id or account_id is required"


class TestRecommendMessages:
    @pytest.mark.parametrize("value", BLANKS)
    def test_recommend_needs_a_feed_id(self, client, value):
        with pytest.raises(IntemptConfigError) as excinfo:
            client.recommend(user_id="u1", feed_id=value, fields=["id"])

        assert message(excinfo) == "recommend: feed_id must be a non-empty string"

    def test_recommend_needs_an_identifier(self, client):
        with pytest.raises(IntemptConfigError) as excinfo:
            client.recommend(feed_id="5292", fields=["id"])

        assert message(excinfo) == "recommend: one of user_id or account_id is required"

    @pytest.mark.parametrize("fields", [[], (), "id", 5])
    def test_recommend_says_what_fields_must_be(self, client, fields):
        with pytest.raises(IntemptConfigError) as excinfo:
            client.recommend(user_id="u1", feed_id="5292", fields=fields)

        assert message(excinfo) == "recommend: fields must be a non-empty sequence"

    @pytest.mark.parametrize("limit", [0, -1, 1.5, "5"])
    def test_recommend_says_what_a_limit_must_be(self, client, limit):
        with pytest.raises(IntemptConfigError) as excinfo:
            client.recommend(user_id="u1", feed_id="5292", fields=["id"], limit=limit)

        assert message(excinfo) == "recommend: limit must be a positive integer"


class TestConfigMessages:
    def base(self, **overrides):
        options = {
            "org": ORG,
            "project": PROJECT,
            "api_key": API_KEY,
        }
        options.update(overrides)
        return options

    @pytest.mark.parametrize("host", ["", "   "])
    def test_an_empty_host_says_so(self, host):
        with pytest.raises(IntemptConfigError) as excinfo:
            Intempt(**self.base(host=host))

        assert message(excinfo) == "host must not be empty"

    def test_an_explicit_none_host_means_the_default_not_an_error(self):
        # None is "not supplied"; an empty string is a mistake. Keeping the two
        # apart is deliberate, so it is asserted rather than left to chance.
        client = Intempt(**self.base(host=None))
        try:
            assert client._config.host == "api.intempt.com"
        finally:
            client.close()

    @pytest.mark.parametrize("host", ["h:abc", "h:0", "h:65536"])
    def test_a_bad_port_quotes_the_host_it_came_from(self, host):
        with pytest.raises(IntemptConfigError) as excinfo:
            Intempt(**self.base(host=host))

        assert message(excinfo) == f"invalid port in host: {host}"

    @pytest.mark.parametrize("timeout", [0, -1, "5"])
    def test_a_bad_timeout_says_positive_seconds(self, timeout):
        with pytest.raises(IntemptConfigError) as excinfo:
            Intempt(**self.base(timeout=timeout))

        assert message(excinfo) == "timeout must be a positive number of seconds"

    @pytest.mark.parametrize("value", [0, -1, 1.5])
    def test_a_bad_max_request_events_says_positive_integer(self, value):
        with pytest.raises(IntemptConfigError) as excinfo:
            Intempt(**self.base(max_request_events=value))

        assert message(excinfo) == "max_request_events must be a positive integer"

    def test_a_bad_batch_type_names_both_accepted_shapes(self):
        with pytest.raises(IntemptConfigError) as excinfo:
            Intempt(**self.base(batch="yes"))

        assert message(excinfo) == "batch must be a BatchOptions or a mapping"

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"size": 0}, "batch.size must be at least 1"),
            ({"flush_ms": 0}, "batch.flush_ms must be at least 1"),
            ({"size": 10, "max_queue": 5}, "batch.max_queue must be at least batch.size"),
        ],
    )
    def test_each_batch_field_names_itself(self, kwargs, expected):
        options = {"size": 10, "flush_ms": 1000, "max_queue": 100}
        options.update(kwargs)

        with pytest.raises(IntemptConfigError) as excinfo:
            Intempt(**self.base(batch=BatchOptions(**options)))

        assert message(excinfo) == expected

    def test_an_unknown_set_config_option_is_listed_by_name(self, client):
        with pytest.raises(IntemptConfigError) as excinfo:
            client.set_config(nonsense=1)

        assert message(excinfo).startswith("set_config: unknown option(s): ")
        assert "nonsense" in message(excinfo)
