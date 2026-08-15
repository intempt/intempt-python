#!/usr/bin/env python3
"""Contract test against a real Intempt environment.

The unit suite proves the SDK sends what it intends to. This proves the platform
accepts it — which is a different question, and the one that bites. A 401 here
means the Basic auth header is rejected; a 400 means a payload shape the mocked
tests were happy with is not what ingestion wants.

Mirrors the Node SDK's `scripts/e2e.mjs` step for step, so a divergence between
the two SDKs shows up as a different PASS/FAIL table rather than as a support
ticket.

Reads credentials from the environment, or from a gitignored `.env.local` beside
the repository root:

    INTEMPT_E2E_API_KEY     a PUBLIC key for a throwaway staging project
    INTEMPT_E2E_ORG
    INTEMPT_E2E_PROJECT
    INTEMPT_E2E_SOURCE_ID
    INTEMPT_E2E_USER_ID     a stable pre-existing test profile
    INTEMPT_E2E_ACCOUNT_ID  a stable test account          (optional)
    INTEMPT_E2E_FEED_ID     a real feed id                 (optional)
    INTEMPT_E2E_FEED_FIELDS comma-separated                (optional)
    INTEMPT_E2E_PRODUCT_ID  an id that exists in the catalog (optional)
    INTEMPT_E2E_HOST        defaults to api.staging.intempt.com
    INTEMPT_E2E_SCHEME      defaults to https

Exit codes: 0 every step passed or skipped, 1 at least one failed, 2 no
credential so nothing ran.

Copyright 2026 Intempt Technologies
Licensed under the Apache License, Version 2.0.
"""

from __future__ import annotations

import datetime as _dt
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from intempt import BatchOptions, Intempt, IntemptApiError  # noqa: E402

# --- .env.local -------------------------------------------------------------


def load_env_file(path: Path) -> int:
    """Fill os.environ from a KEY=value file. Existing values win."""
    if not path.is_file():
        return 0
    loaded = 0
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


loaded_count = load_env_file(Path(__file__).resolve().parent.parent / ".env.local")

# --- inputs -----------------------------------------------------------------

API_KEY = os.environ.get("INTEMPT_E2E_API_KEY", "")
ORG = os.environ.get("INTEMPT_E2E_ORG", "")
PROJECT = os.environ.get("INTEMPT_E2E_PROJECT", "")
SOURCE_ID = os.environ.get("INTEMPT_E2E_SOURCE_ID") or None
USER_ID = os.environ.get("INTEMPT_E2E_USER_ID") or None
ACCOUNT_ID = os.environ.get("INTEMPT_E2E_ACCOUNT_ID") or None
FEED_ID = os.environ.get("INTEMPT_E2E_FEED_ID") or None
PRODUCT_ID = os.environ.get("INTEMPT_E2E_PRODUCT_ID") or None
HOST = os.environ.get("INTEMPT_E2E_HOST") or "api.staging.intempt.com"
SCHEME = os.environ.get("INTEMPT_E2E_SCHEME") or "https"
FEED_FIELDS = [
    field.strip()
    for field in (os.environ.get("INTEMPT_E2E_FEED_FIELDS") or "id").split(",")
    if field.strip()
]

if not API_KEY or not ORG or not PROJECT:
    print(
        "INTEMPT_E2E_API_KEY, INTEMPT_E2E_ORG and INTEMPT_E2E_PROJECT are required.\n"
        "Set them in the environment or in a gitignored .env.local at the repo root.",
        file=sys.stderr,
    )
    raise SystemExit(2)

# An ephemeral id still exercises every write path; a stable one keeps the test
# project from filling up with single-event profiles.
user_id = USER_ID or f"sdk-e2e-{uuid.uuid4().hex[:12]}@example.com"

# --- readiness --------------------------------------------------------------

print(f"\nIntempt Python SDK contract test — {HOST}")
if loaded_count:
    print(f"  loaded {loaded_count} value(s) from .env.local")
print(f"  profile: {user_id}{' (stable)' if USER_ID else ' (ephemeral)'}\n")
print("  project inputs")
print("  " + "-" * 76)
for name, value, used_by in [
    ("stable user_id", USER_ID, "identify, track, group, alias, consent"),
    ("account_id (optional)", ACCOUNT_ID, "group — created automatically if absent"),
    ("feed id", FEED_ID, "recommend"),
    ("product_id", PRODUCT_ID, "ecommerce.*"),
]:
    print(f"  {'have' if value else 'MISS'}  {name:<22} {used_by}")
print("  " + "-" * 76 + "\n")

client = Intempt(
    org=ORG,
    project=PROJECT,
    api_key=API_KEY,
    source_id=SOURCE_ID,
    host=HOST,
    scheme=SCHEME,
)

# --- harness ----------------------------------------------------------------

results: list[dict[str, object]] = []


def step(name: str, fn) -> None:
    started = time.monotonic()
    try:
        value = fn()
        ms = int((time.monotonic() - started) * 1000)
        note = "2xx" if value is None else str(value)[:60]
        results.append({"name": name, "state": "PASS", "ms": ms, "note": note})
        print(f"  PASS  {name:<46} {ms:>5}ms  {note}")
    except IntemptApiError as error:
        ms = int((time.monotonic() - started) * 1000)
        status = error.status if error.status is not None else "transport"
        body = (error.body or "")[:160]
        results.append({"name": name, "state": "FAIL", "ms": ms, "note": f"{status}: {body}"})
        print(f"  FAIL  {name:<46} {ms:>5}ms  {status} {body}")
    except Exception as error:  # noqa: BLE001 - reported, not swallowed
        ms = int((time.monotonic() - started) * 1000)
        results.append({"name": name, "state": "FAIL", "ms": ms, "note": str(error)[:160]})
        print(f"  FAIL  {name:<46} {ms:>5}ms  {error}")


def skip(name: str, why: str) -> None:
    results.append({"name": name, "state": "SKIP", "ms": 0, "note": why})
    print(f"  SKIP  {name:<46}         {why}")


# --- writes -----------------------------------------------------------------

# A 401 here means the Basic auth header is rejected, which is the single most
# important thing this test exists to prove.
step(
    "identify (proves Basic auth is accepted)",
    lambda: client.identify(user_id=user_id, traits={"source": "sdk-e2e"}),
)

step(
    "track",
    lambda: client.track("sdk_e2e_event", user_id=user_id, properties={"runner": "python"}),
)

# Inside the 2010..2040 window ingestion accepts. Outside it, the low end is
# rejected and the high end is silently replaced with the server clock — so a
# pass here is also evidence the SDK is sending milliseconds, not seconds.
step(
    "track with an explicit timestamp",
    lambda: client.track(
        "sdk_e2e_backfill",
        user_id=user_id,
        timestamp=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1),
    ),
)

step(
    "track_batch (2 events, 1 request)",
    lambda: client.track_batch(
        [
            {"event": "sdk_e2e_batch_a", "user_id": user_id},
            {"event": "sdk_e2e_batch_b", "user_id": user_id},
        ]
    ),
)

step(
    "group (creates the account if absent)",
    lambda: client.group(
        user_id=user_id,
        account_id=ACCOUNT_ID or "sdk-e2e-account",
        attributes={"tier": "e2e"},
    ),
)

step(
    "alias",
    lambda: client.alias(user_id=user_id, previous_user_id=f"sdk-e2e-prev-{uuid.uuid4().hex[:8]}"),
)

# --- commerce ---------------------------------------------------------------

# Ingestion answers 201 for a product id that does not exist, so a pass with a
# made-up id proves only that the request was well formed. With a real catalog
# id it proves the line actually resolves.
commerce_product = PRODUCT_ID or "sdk-e2e-product"
suffix = " (catalog product)" if PRODUCT_ID else ""

step(
    f"ecommerce.product_viewed{suffix}",
    lambda: client.ecommerce.product_viewed(user_id=user_id, product_id=commerce_product),
)
step(
    f"ecommerce.added_to_cart{suffix}",
    lambda: client.ecommerce.added_to_cart(
        user_id=user_id, product_id=commerce_product, quantity=2
    ),
)
step(
    f"ecommerce.ordered{suffix} (1 line)",
    lambda: client.ecommerce.ordered(
        user_id=user_id, products=[{"product_id": commerce_product, "quantity": 1}]
    ),
)

# --- consent ----------------------------------------------------------------

# /consents/data takes epoch SECONDS while /track takes milliseconds. Sending
# milliseconds here lands past 2040 and is silently rewritten, so this step is
# the only proof the SDK gets the unit right.
step(
    "consent.grant (proves epoch-seconds timestamps)",
    lambda: client.consent.grant(user_id=user_id, category="marketing"),
)
step(
    "consent.revoke",
    lambda: client.consent.revoke(user_id=user_id, reason="sdk-e2e teardown"),
)

# --- reads ------------------------------------------------------------------

if FEED_ID:

    def real_feed() -> str:
        items = client.recommend(user_id=user_id, feed_id=FEED_ID, fields=FEED_FIELDS, limit=3)
        return f"{len(items or [])} item(s)"

    step("recommend (real feed resolves)", real_feed)

    # The negative case matters as much: if an unknown feed also returns 200,
    # a typo'd feed id degrades silently to an empty page forever.
    def unknown_feed() -> str:
        try:
            client.recommend(user_id=user_id, feed_id="000000000", fields=FEED_FIELDS, limit=1)
        except IntemptApiError as error:
            # 401/403 means the credential was refused, which says nothing about
            # the feed. Accepting it made this step pass during a run where every
            # single call 401'd — a green tick for a test that proved nothing.
            if error.status in (401, 403):
                raise AssertionError(
                    f"got {error.status} (auth), so the feed was never evaluated"
                ) from error
            return f"rejected with {error.status}, as it should be"
        raise AssertionError("an unknown feed id returned success")

    step("recommend (unknown feed is rejected)", unknown_feed)
else:
    skip("recommend", "no INTEMPT_E2E_FEED_ID")

# --- buffered mode ----------------------------------------------------------


class DeliveryWatcher:
    """Captures what the buffer logged, so a dropped batch cannot read as success.

    The retry table drops a non-retryable 4xx batch and logs it rather than
    raising — right for a background buffer, wrong for a contract test.
    `flush()` returned cleanly during a run where every request 401'd, and this
    step reported PASS having delivered nothing.
    """

    def __init__(self) -> None:
        self.errors: list[str] = []

    def error(self, message: object, *args: object, **kwargs: object) -> None:
        self.errors.append(str(message))

    def warning(self, message: object, *args: object, **kwargs: object) -> None:
        self.errors.append(str(message))

    def info(self, *args: object, **kwargs: object) -> None:
        return None

    def debug(self, *args: object, **kwargs: object) -> None:
        return None


def buffered() -> str:
    watcher = DeliveryWatcher()
    buffered_client = Intempt(
        org=ORG,
        project=PROJECT,
        api_key=API_KEY,
        source_id=SOURCE_ID,
        host=HOST,
        scheme=SCHEME,
        batch=BatchOptions(size=50, flush_ms=60_000, max_queue=1_000),
        logger=watcher,
    )
    try:
        for index in range(5):
            buffered_client.track("sdk_e2e_buffered", user_id=user_id, properties={"i": index})
        buffered_client.flush()
        if watcher.errors:
            raise AssertionError(f"flush dropped the batch: {watcher.errors[0][:120]}")
        return "5 events, 1 request"
    finally:
        buffered_client.close()


step("flush (5 events buffered, 1 request)", buffered)

client.close()

# --- report -----------------------------------------------------------------

passed = sum(1 for r in results if r["state"] == "PASS")
failed = sum(1 for r in results if r["state"] == "FAIL")
skipped = sum(1 for r in results if r["state"] == "SKIP")

print("\n  " + "-" * 76)
print(f"  {passed} passed, {failed} failed, {skipped} skipped")
print("  " + "-" * 76 + "\n")

if failed:
    print("  failures:")
    for result in results:
        if result["state"] == "FAIL":
            print(f"    {result['name']}: {result['note']}")
    print()

raise SystemExit(1 if failed else 0)
