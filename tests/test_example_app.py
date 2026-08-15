"""The sample app is executed, not just shipped.

A sample nobody runs is documentation that rots. This starts the real app in a
subprocess, points it at the loopback test server, drives every route with curl-
equivalent requests, and asserts the events actually arrived.

It also covers the shutdown path, which is the part of a sample most likely to be
wrong and least likely to be noticed: if close() is not reached, the buffered
events are lost and nobody sees an error.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import intempt
from tests.conftest import API_KEY, ORG, PROJECT, SOURCE


def _find(relative: str) -> Path:
    """Walk up from this file until `relative` turns up.

    mutmut runs the suite from a `mutants/` copy that holds only the package and
    the tests, so anything resolved as "two directories up" lands in a tree where
    `examples/` does not exist. Searching upward finds the real one from either
    layout.
    """
    for directory in Path(__file__).resolve().parents:
        candidate = directory / relative
        if candidate.exists():
            return candidate
    raise RuntimeError(f"could not locate {relative} from {__file__}")


APP = _find("examples/basic/app.py")

# The `src` the test process actually imported, which under mutmut is the mutated
# copy. Deriving it from the module rather than from a path keeps the subprocess
# exercising the same code as the rest of the suite.
SRC = Path(intempt.__file__).resolve().parent.parent


def free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def post(url: str, form: str) -> tuple[int, dict]:
    request = urllib.request.Request(
        url, data=form.encode(), headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


@pytest.fixture
def app(server):
    """The real sample app, pointed at the loopback server."""
    server.reset()
    port = free_port()

    env = {
        **os.environ,
        "INTEMPT_ORG": ORG,
        "INTEMPT_PROJECT": PROJECT,
        "INTEMPT_API_KEY": API_KEY,
        "INTEMPT_SOURCE_ID": SOURCE,
        "INTEMPT_FEED_ID": "5292",
        # The sample hardcodes api.intempt.com; these two make it talk to the
        # test server instead. Anything the sample cannot be pointed at is
        # something a customer cannot test either.
        "INTEMPT_HOST": server.host,
        "INTEMPT_SCHEME": "http",
        "SAMPLE_PORT": str(port),
        "PYTHONPATH": str(SRC),
    }

    process = subprocess.Popen(
        [sys.executable, str(APP)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            pytest.fail(f"sample app exited early:\n{output}")
        try:
            # Probe an inert route. /recommend would fire a real request at the
            # test server, and then a later wait-for-one-request returns on the
            # probe's own traffic instead of the event under test — which is
            # exactly how the first version of this file passed while asserting
            # nothing.
            urllib.request.urlopen(base + "/__ready", timeout=0.3)
            break
        except urllib.error.HTTPError:
            break  # responding with 404 is all we need
        except OSError:
            time.sleep(0.1)
    else:
        process.kill()
        pytest.fail("sample app never became ready")

    # Start each test from a clean slate, after readiness rather than before.
    server.reset()
    yield base, process

    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover
        process.kill()


class TestSampleApp:
    def test_signup_sends_identify_and_track(self, app, server):
        base, _ = app
        status, body = post(base + "/signup", "user=ada@example.com&plan=pro")
        assert status == 201
        assert body == {"ok": True}

        names = _wait_for_event(server, "signed_up")
        assert "Identify" in names
        assert "signed_up" in names

    def test_purchase_sends_a_commerce_event(self, app, server):
        base, _ = app
        status, _ = post(base + "/purchase", "user=ada@example.com&sku=21&qty=2")
        assert status == 201

        names = _wait_for_event(server, "Product ordered")
        assert "Product ordered" in names

    def test_purchase_without_a_sku_is_rejected_before_sending(self, app, server):
        base, _ = app
        status, body = post(base + "/purchase", "user=ada@example.com")
        assert status == 400
        assert "sku" in body["error"]

    def test_missing_user_is_rejected(self, app):
        base, _ = app
        status, body = post(base + "/signup", "plan=pro")
        assert status == 400
        assert "user" in body["error"]

    def test_forget_records_a_consent_revocation(self, app, server):
        base, _ = app
        status, _ = post(base + "/forget", "user=ada@example.com")
        assert status == 202

        _wait_for(server, 1)
        consent = [r for r in server.requests if r.path.endswith("/consents/data")]
        assert consent and consent[0].body["action"] == "reject"

    def test_recommend_degrades_rather_than_failing_the_page(self, app, server):
        """A feed error must not become a 500 for the customer."""
        base, _ = app
        server.expect_error = None  # no reply scripted; the default 200 is fine
        status, body = get(base + "/recommend?user=ada@example.com")
        assert status == 200
        assert "items" in body

    def test_unknown_route_is_a_404(self, app):
        base, _ = app
        assert post(base + "/nope", "user=x")[0] == 404

    def test_shutdown_drains_the_buffer(self, app, server):
        """The part of a sample most likely to be wrong and least likely noticed."""
        base, process = app
        for index in range(5):
            post(base + "/signup", f"user=u{index}@example.com")

        before = len(server.requests)
        process.terminate()
        process.wait(timeout=15)

        _wait_for(server, before + 1, timeout=5)
        assert len(server.requests) > before, "close() did not drain on shutdown"


def _wait_for(server, count: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(server.requests) >= count:
            return
        time.sleep(0.05)


def _wait_for_event(server, name: str, timeout: float = 8.0) -> list[str]:
    """Wait for a named event rather than for a request count.

    A count is satisfied by any traffic, including a readiness probe, so it can
    return before the thing under test has been sent.
    """
    deadline = time.time() + timeout
    names: list[str] = []
    while time.time() < deadline:
        names = [e["name"] for r in server.requests for e in r.body.get("track", [])]
        if name in names:
            return names
        time.sleep(0.05)
    return names
