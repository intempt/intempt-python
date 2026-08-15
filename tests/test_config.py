"""Configuration, credentials and transport boundaries.

The validation checks decide whether bad input is refused at construction or
carried silently onto the wire, so each boundary is pinned rather than assumed.
"""

from __future__ import annotations

import base64
import datetime as _dt

import pytest

from intempt import ApiKeyCredentials, BatchOptions, IntemptConfigError
from intempt._config import merge_config, resolve_config
from intempt._transport import parse_retry_after
from intempt._util import chunk, compact, ensure_timestamp
from tests.conftest import API_KEY, ORG, PROJECT, SOURCE


def base(**overrides):
    options = {"org": ORG, "project": PROJECT, "api_key": API_KEY}
    options.update(overrides)
    return options


class TestRequiredOptions:
    @pytest.mark.parametrize("field", ["org", "project", "api_key"])
    def test_names_the_missing_field(self, field):
        options = base()
        del options[field]
        with pytest.raises(IntemptConfigError, match=f'"{field}" is required'):
            resolve_config(**options)

    @pytest.mark.parametrize("value", ["", "   ", None, 42])
    def test_rejects_a_blank_or_wrong_typed_org(self, value):
        with pytest.raises(IntemptConfigError, match='"org" is required'):
            resolve_config(**base(org=value))


class TestSourceId:
    def test_a_19_digit_id_survives_intact(self):
        """int() would round the last digits and address another source."""
        config = resolve_config(**base(source_id=SOURCE))
        assert config.source_id == SOURCE
        assert isinstance(config.source_id, str)

    def test_a_numeric_id_is_stringified_not_coerced(self):
        config = resolve_config(**base(source_id=684508596718616576))
        assert config.source_id == "684508596718616576"

    def test_rejects_a_whitespace_only_id(self):
        with pytest.raises(IntemptConfigError, match='"source_id" must not be empty'):
            resolve_config(**base(source_id="   "))

    def test_absent_when_not_provided(self):
        assert resolve_config(**base()).source_id is None


class TestHostAndPort:
    def test_splits_host_and_port(self):
        config = resolve_config(**base(host="api.test.local:8443"))
        assert config.host == "api.test.local"
        assert config.port == 8443

    def test_no_port_when_the_host_carries_none(self):
        assert resolve_config(**base(host="api.test.local")).port is None

    @pytest.mark.parametrize(
        "host", ["api.test.local:0", "api.test.local:abc", "api.test.local:99999"]
    )
    def test_rejects_an_unusable_port(self, host):
        with pytest.raises(IntemptConfigError, match="invalid port in host"):
            resolve_config(**base(host=host))

    @pytest.mark.parametrize("host", ["", "   ", ":8443"])
    def test_rejects_an_empty_host(self, host):
        with pytest.raises(IntemptConfigError, match="host must not be empty"):
            resolve_config(**base(host=host))


class TestOtherOptions:
    @pytest.mark.parametrize("timeout", [0, -1, "10"])
    def test_rejects_a_non_positive_timeout(self, timeout):
        with pytest.raises(IntemptConfigError, match="timeout must be a positive number"):
            resolve_config(**base(timeout=timeout))

    def test_rejects_an_unsupported_scheme(self):
        with pytest.raises(IntemptConfigError, match='unsupported scheme "ftp"'):
            resolve_config(**base(scheme="ftp"))

    @pytest.mark.parametrize("value", [0, -5, 2.5])
    def test_rejects_a_bad_max_request_events(self, value):
        with pytest.raises(
            IntemptConfigError, match="max_request_events must be a positive integer"
        ):
            resolve_config(**base(max_request_events=value))

    def test_rejects_a_logger_missing_a_level(self):
        class Partial:
            def debug(self, *_): ...
            def info(self, *_): ...

        with pytest.raises(IntemptConfigError, match="missing: warning, error"):
            resolve_config(**base(logger=Partial()))

    def test_builds_the_project_path(self):
        config = resolve_config(**base(org="a b", project="c/d"))
        assert config.project_path("/track") == "/v1/a%20b/projects/c%2Fd/track"


class TestBatchOptions:
    def test_rejects_a_size_below_one(self):
        with pytest.raises(IntemptConfigError, match="batch.size must be at least 1"):
            BatchOptions(size=0)

    def test_rejects_a_flush_interval_below_one(self):
        with pytest.raises(IntemptConfigError, match="batch.flush_ms must be at least 1"):
            BatchOptions(flush_ms=0)

    def test_rejects_a_queue_smaller_than_the_batch(self):
        with pytest.raises(IntemptConfigError, match="max_queue must be at least batch.size"):
            BatchOptions(size=10, max_queue=5)

    def test_accepts_a_mapping(self):
        config = resolve_config(**base(batch={"size": 5, "flush_ms": 100, "max_queue": 10}))
        assert config.batch.size == 5


class TestSetConfig:
    def test_changes_a_mutable_option(self):
        config = resolve_config(**base())
        assert merge_config(config, {"timeout": 5.0}).timeout == 5.0

    @pytest.mark.parametrize("field", ["org", "project", "api_key", "source_id", "keep_alive"])
    def test_refuses_a_fixed_option(self, field):
        config = resolve_config(**base())
        with pytest.raises(IntemptConfigError, match=f'"{field}" is fixed at construction'):
            merge_config(config, {field: "x"})

    def test_clears_a_port_when_the_new_host_has_none(self):
        """Otherwise the next request goes to new-host:old-port."""
        config = resolve_config(**base(host="a.test:8443"))
        assert merge_config(config, {"host": "b.test"}).port is None

    def test_keeps_a_port_the_new_host_carries(self):
        config = resolve_config(**base(host="a.test:8443"))
        assert merge_config(config, {"host": "b.test:9000"}).port == 9000

    def test_rejects_an_unknown_option(self):
        config = resolve_config(**base())
        with pytest.raises(IntemptConfigError, match="unknown option"):
            merge_config(config, {"nope": 1})


class TestCredentials:
    def test_encodes_basic_auth(self):
        header = ApiKeyCredentials(API_KEY).authorization_header()
        expected = base64.b64encode(b"pfx0123456789abcdef:sec0123456789abcdef").decode()
        assert header == f"Basic {expected}"

    @pytest.mark.parametrize("key", ["nodot", "", ".secret", "prefix.", 42])
    def test_rejects_a_malformed_key(self, key):
        with pytest.raises(IntemptConfigError, match="<prefix>.<secret>"):
            ApiKeyCredentials(key)

    def test_repr_redacts_the_secret(self):
        text = repr(ApiKeyCredentials(API_KEY))
        assert "sec0123456789abcdef" not in text
        assert "redacted" in text

    def test_str_redacts_the_secret(self):
        assert "sec0123456789abcdef" not in str(ApiKeyCredentials(API_KEY))

    def test_refuses_to_pickle(self):
        import pickle

        with pytest.raises(TypeError, match="not serialisable"):
            pickle.dumps(ApiKeyCredentials(API_KEY))


class TestRetryAfter:
    def test_parses_seconds(self):
        assert parse_retry_after("3") == 3000

    def test_zero_is_zero_not_none(self):
        assert parse_retry_after("0") == 0

    @pytest.mark.parametrize("value", [None, "", "not-a-date", "-5", "nan", "inf"])
    def test_unusable_values_yield_none(self, value):
        assert parse_retry_after(value) is None

    def test_parses_an_http_date(self):
        future = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=30)
        stamp = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
        parsed = parse_retry_after(stamp)
        assert parsed is not None and 20_000 < parsed < 40_000

    def test_a_past_http_date_is_never_negative(self):
        past = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)
        stamp = past.strftime("%a, %d %b %Y %H:%M:%S GMT")
        assert parse_retry_after(stamp) == 0


class TestUtilities:
    def test_ensure_timestamp_accepts_epoch_millis(self):
        assert ensure_timestamp(1_760_000_000_000) == 1_760_000_000_000

    def test_ensure_timestamp_treats_a_naive_datetime_as_utc(self):
        naive = _dt.datetime(2026, 8, 15, 12, 0)
        aware = _dt.datetime(2026, 8, 15, 12, 0, tzinfo=_dt.timezone.utc)
        assert ensure_timestamp(naive) == ensure_timestamp(aware)

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), "2026-01-01", None, True])
    def test_ensure_timestamp_rejects_unusable_input(self, value):
        with pytest.raises(IntemptConfigError, match="timestamp must be"):
            ensure_timestamp(value)

    def test_chunk_splits_with_a_remainder(self):
        assert chunk([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]

    @pytest.mark.parametrize("size", [0, -1])
    def test_chunk_rejects_a_size_that_cannot_advance(self, size):
        with pytest.raises(IntemptConfigError, match="chunk size must be at least 1"):
            chunk([1, 2], size)

    def test_compact_drops_only_none(self):
        assert compact({"a": None, "b": 0, "c": "", "d": False}) == {"b": 0, "c": "", "d": False}


class TestConfigSnapshot:
    def test_config_is_frozen(self, client):
        import dataclasses

        config = client().config
        with pytest.raises(dataclasses.FrozenInstanceError):
            config.host = "elsewhere"  # type: ignore[misc]

    def test_buffered_is_zero_without_batching(self, client):
        assert client().buffered == 0
