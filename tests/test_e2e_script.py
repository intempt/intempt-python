"""scripts/e2e.py must fail a write the platform refused (Ro-bot python#16 impact:F2).

Writes report a delivery problem through their return value instead of raising (INT-7444), so a
harness that counts "did not raise" as PASS reports a green table against a dead endpoint.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import API_KEY, ORG, PROJECT, SOURCE, Reply

#: Spawns a subprocess. Excluded from the mutation run; see pyproject.
pytestmark = pytest.mark.sample

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "e2e.py"


def run_script(server) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "INTEMPT_E2E_API_KEY": API_KEY,
        "INTEMPT_E2E_ORG": ORG,
        "INTEMPT_E2E_PROJECT": PROJECT,
        "INTEMPT_E2E_SOURCE_ID": SOURCE,
        "INTEMPT_E2E_HOST": server.host,
        "INTEMPT_E2E_SCHEME": "http",
        # Present but empty, so a developer's .env.local cannot fill them in.
        "INTEMPT_E2E_USER_ID": "",
        "INTEMPT_E2E_ACCOUNT_ID": "",
        "INTEMPT_E2E_FEED_ID": "",
        "INTEMPT_E2E_PRODUCT_ID": "",
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60
    )


def test_a_refused_write_fails_the_run(server):
    server.reset()
    server.expect(*[Reply(status=500, body="boom") for _ in range(40)])
    try:
        result = run_script(server)
    finally:
        server.reset()

    assert result.returncode == 1, result.stdout
    for name in ("identify", "track", "group", "consent.grant", "consent.revoke"):
        assert f"FAIL  {name}" in result.stdout, result.stdout
    assert "FAIL  flush" in result.stdout, result.stdout
    assert "terminal 500" in result.stdout, result.stdout
    assert " PASS " not in result.stdout, result.stdout


def test_an_accepted_run_passes(server):
    server.reset()
    try:
        result = run_script(server)
    finally:
        server.reset()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 failed" in result.stdout, result.stdout
