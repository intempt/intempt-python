"""Runs `examples/bare/send.py` as a real process against the recording server.

The sample is documentation that executes, so it is tested like code. A sample
that has drifted from the SDK teaches the wrong thing to whoever copies it, and
the only way to know it still works is to run it.

Copyright 2026 Intempt Technologies
Licensed under the Apache License, Version 2.0.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from tests.conftest import API_KEY, ORG, PROJECT, SOURCE, Reply
from tests.test_example_app import SRC, _find

BARE = _find("examples/bare/send.py")

USER = "bare-test@example.com"


def run(server: object, **overrides: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(SRC),
        "PYTHONUNBUFFERED": "1",
        "INTEMPT_ORG": ORG,
        "INTEMPT_PROJECT": PROJECT,
        "INTEMPT_API_KEY": API_KEY,
        "INTEMPT_SOURCE_ID": SOURCE,
        "INTEMPT_USER_ID": USER,
        "INTEMPT_HOST": server.host,  # type: ignore[attr-defined]
        "INTEMPT_SCHEME": "http",
    }
    env.update(overrides)
    env = {k: v for k, v in env.items() if v != ""}

    return subprocess.run(
        [sys.executable, str(BARE)], env=env, capture_output=True, text=True, timeout=60
    )


@pytest.fixture(autouse=True)
def _reset(server: object) -> None:
    server.reset()  # type: ignore[attr-defined]


class TestBareSample:
    def test_the_file_the_readme_names_exists(self):
        assert BARE.exists()
        assert BARE.name == "send.py"

    def test_it_exits_zero_and_sends_every_call(self, server):
        server.expect(*[Reply(status=200, body="{}") for _ in range(20)])

        result = run(server)

        assert result.returncode == 0, result.stdout + result.stderr
        assert USER in result.stdout

        names = [request.path for request in server.requests]
        assert names, "the sample sent nothing"

    def test_every_request_carries_the_credential_and_the_user(self, server):
        server.expect(*[Reply(status=200, body="{}") for _ in range(20)])

        run(server)

        assert server.requests
        for request in server.requests:
            assert request.headers.get("authorization", "").startswith("Basic ")
            assert request.headers.get("x-intempt-lib", "").startswith("intempt-python/")

    def test_it_sends_the_identify_track_group_and_commerce_calls(self, server):
        server.expect(*[Reply(status=200, body="{}") for _ in range(20)])

        run(server)

        blob = "".join(str(request.body) for request in server.requests)
        for expected in ("purchase", "acme-inc", "sku-1", "marketing"):
            assert expected in blob, f"{expected!r} never reached the wire"

    def test_the_source_id_keeps_all_nineteen_digits(self, server):
        server.expect(*[Reply(status=200, body="{}") for _ in range(20)])

        run(server)

        blob = "".join(str(request.body) + request.path for request in server.requests)
        assert SOURCE in blob, "a numeric round trip would have dropped the last digits"

    def test_missing_environment_exits_two_and_says_what_is_missing(self, server):
        result = run(server, INTEMPT_API_KEY="")

        assert result.returncode == 2
        assert "INTEMPT_API_KEY" in result.stderr
        assert server.requests == [], "nothing may be sent without a credential"

    def test_a_bad_argument_exits_two_rather_than_one(self, server):
        # An empty org is refused at construction, before any request.
        result = run(server, INTEMPT_ORG="   ")

        assert result.returncode == 2
        assert "bad arguments" in result.stderr or "org" in result.stderr
        assert server.requests == []

    def test_an_api_failure_exits_one_and_reports_the_status(self, server):
        server.expect(*[Reply(status=500, body='{"error": "nope"}') for _ in range(20)])

        result = run(server)

        assert result.returncode == 1
        assert "status=500" in result.stderr
        assert "retryable=True" in result.stderr

    def test_a_401_is_reported_as_not_retryable(self, server):
        server.expect(*[Reply(status=401, body="{}") for _ in range(20)])

        result = run(server)

        assert result.returncode == 1
        assert "status=401" in result.stderr
        assert "retryable=False" in result.stderr

    def test_recommend_is_skipped_without_a_feed_id(self, server):
        server.expect(*[Reply(status=200, body="{}") for _ in range(20)])

        result = run(server)

        assert "recommend" not in result.stdout
        assert not any("/feeds" in request.path for request in server.requests)

    def test_a_feed_id_turns_recommend_on(self, server):
        server.expect(*[Reply(status=200, body='{"items": []}') for _ in range(20)])

        result = run(server, INTEMPT_FEED_ID="5292")

        assert result.returncode == 0
        assert "recommend" in result.stdout

    def test_a_failing_recommend_degrades_instead_of_failing_the_run(self, server):
        # Every other call succeeds; only the feed read fails. The sample must
        # still exit 0, because a recommendation is an enhancement.
        #
        # The sample makes exactly 8 requests: six track calls, one consent
        # record, then the feed. Counted from the server rather than assumed —
        # the first version of this test guessed nine and quietly let the feed
        # read succeed, so it asserted nothing.
        server.expect(*[Reply(status=200, body="{}") for _ in range(7)])
        server.expect(*[Reply(status=503, body="{}") for _ in range(4)])

        result = run(server, INTEMPT_FEED_ID="5292")

        assert result.returncode == 0, result.stdout + result.stderr
        assert "default order" in result.stdout
