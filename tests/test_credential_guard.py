"""The credential never appears in a dump of anything the SDK hands back.

This exists because the same defect appeared independently in two SDKs: PHP
exposed the raw key as a public readonly property, and Python exposed it on the
resolved config. Both printed the secret through an ordinary repr/print_r of an
object a caller legitimately holds.

Node was clean by luck of its type shape rather than by design. The equivalent
of this file now exists in all three repos, so "clean" stops being luck.
"""

from __future__ import annotations

import pickle
import pprint

import pytest

from intempt import ApiKeyCredentials, Intempt, IntemptConfigError

SECRET = "sec0123456789abcdef"
KEY = f"pfx0123456789abcdef.{SECRET}"


def client() -> Intempt:
    return Intempt(org="o", project="p", api_key=KEY, source_id="1841503112918048768")


def views_of(value: object) -> list[str]:
    """Every ordinary way a value ends up in a log or a traceback."""
    return [repr(value), str(value), pprint.pformat(value), f"{value}", f"{value!r}"]


class TestConfigSnapshot:
    def test_no_view_of_the_config_contains_the_secret(self):
        for view in views_of(client().config):
            assert SECRET not in view

    def test_the_config_does_not_expose_a_raw_key_attribute(self):
        # The regression this guards: an `api_key: str` field on the resolved
        # config, which repr() then prints in full.
        assert not hasattr(client().config, "api_key")

    def test_the_config_still_carries_a_usable_credential(self):
        # The fix must not have removed the credential, only the raw form.
        assert client().config.credentials.authorization_header().startswith("Basic ")


class TestCredentialsObject:
    def test_no_view_of_the_credentials_contains_the_secret(self):
        for view in views_of(ApiKeyCredentials(KEY)):
            assert SECRET not in view

    def test_it_says_it_is_redacted_rather_than_looking_empty(self):
        # An empty-looking repr reads as a bug; "redacted" reads as a decision.
        assert "redacted" in repr(ApiKeyCredentials(KEY))

    def test_the_prefix_survives_for_support_purposes(self):
        # Enough to identify which key is in play without revealing it.
        assert "pfx0123456789abcdef" in repr(ApiKeyCredentials(KEY))

    def test_it_refuses_to_pickle(self):
        with pytest.raises(TypeError, match="not serialisable"):
            pickle.dumps(ApiKeyCredentials(KEY))


class TestClientObject:
    def test_no_view_of_the_client_contains_the_secret(self):
        for view in views_of(client()):
            assert SECRET not in view

    def test_the_encoded_form_does_not_leak_either(self):
        # Base64 is not encryption; the header is as sensitive as the key.
        import base64

        encoded = base64.b64encode(KEY.replace(".", ":").encode()).decode()
        for view in views_of(client().config) + views_of(ApiKeyCredentials(KEY)):
            assert encoded not in view


class TestErrorSurfaces:
    def test_a_config_error_never_echoes_the_key(self):
        # Error messages name the field, never the value: the value may be the
        # credential, and messages end up in logs.
        with pytest.raises(IntemptConfigError) as caught:
            Intempt(org="o", project="p", api_key="no-dot-here")
        assert "no-dot-here" not in str(caught.value)
        assert "<prefix>.<secret>" in str(caught.value)
