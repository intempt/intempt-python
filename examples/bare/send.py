"""The smallest thing that works: one client, one of each call, then close.

No web server, no framework, no batching. If you are evaluating the SDK or
debugging credentials, start here — every line is a call you would make in real
code, and the whole file runs in about a second.

    export INTEMPT_ORG=my-org
    export INTEMPT_PROJECT=my-project
    export INTEMPT_API_KEY='prefix.secret'
    export INTEMPT_SOURCE_ID=684508596718616576   # optional
    export INTEMPT_FEED_ID=5292                   # optional, enables recommend
    python examples/bare/send.py

Every call sends one request and returns when the server answers, so the events
are in the console by the time this exits. Open Sources -> your source -> Live
events and look for the user id printed at the end.

Copyright 2026 Intempt Technologies
Licensed under the Apache License, Version 2.0.
"""

from __future__ import annotations

import os
import sys

from intempt import Intempt, IntemptApiError, IntemptConfigError


def main() -> int:
    missing = [
        name
        for name in ("INTEMPT_ORG", "INTEMPT_PROJECT", "INTEMPT_API_KEY")
        if not os.environ.get(name)
    ]
    if missing:
        print("missing environment: " + ", ".join(missing), file=sys.stderr)
        return 2

    user_id = os.environ.get("INTEMPT_USER_ID", "bare-sample@example.com")

    # host and scheme come from the environment so this file can be pointed at a
    # local server. A sample nobody can point elsewhere is a sample nobody can
    # test, including whoever wrote it.
    #
    # Construction validates, so it gets the same handling as the calls below.
    # Leaving it outside the try turned a blank INTEMPT_ORG into a traceback
    # instead of the one-line message this prints for every other bad argument.
    try:
        client = Intempt(
            org=os.environ["INTEMPT_ORG"],
            project=os.environ["INTEMPT_PROJECT"],
            api_key=os.environ["INTEMPT_API_KEY"],
            source_id=os.environ.get("INTEMPT_SOURCE_ID"),
            host=os.environ.get("INTEMPT_HOST") or "api.intempt.com",
            scheme=os.environ.get("INTEMPT_SCHEME") or "https",
        )
    except IntemptConfigError as error:
        print(f"bad arguments: {error}", file=sys.stderr)
        return 2

    # `with` closes the client on the way out. Without batching there is nothing
    # buffered to lose, but closing releases the connection either way.
    with client:
        try:
            # Who this is, and what you know about them.
            client.identify(user_id=user_id, traits={"plan": "pro"})

            # Something they did. `properties` is yours to shape.
            client.track(
                "purchase",
                user_id=user_id,
                properties={"total": 99.99, "currency": "USD"},
            )

            # The company they belong to, if you sell to companies.
            client.group(
                user_id=user_id,
                account_id="acme-inc",
                attributes={"tier": "enterprise"},
            )

            # Commerce events use reserved names the platform reports on, so
            # they cannot be typo'd into a name nothing aggregates.
            client.ecommerce.product_viewed(user_id=user_id, product_id="sku-1")
            client.ecommerce.added_to_cart(user_id=user_id, product_id="sku-1", quantity=2)
            client.ecommerce.ordered(
                user_id=user_id,
                products=[{"product_id": "sku-1", "quantity": 2}],
            )

            # A consent record is explicit and separate from opt_out(), which
            # only gates this client.
            client.consent.grant(user_id=user_id, category="marketing")

            feed_id = os.environ.get("INTEMPT_FEED_ID")
            if feed_id:
                # Treat a recommendation as an enhancement: if it fails, fall
                # back to your own ordering rather than failing the page.
                try:
                    feed = client.recommend(
                        user_id=user_id, feed_id=feed_id, fields=["id"], limit=3
                    )
                    print(f"recommend  -> {feed}")
                except IntemptApiError as error:
                    print(f"recommend  -> unavailable ({error}), using the default order")

        except IntemptConfigError as error:
            # Bad arguments. Never retried, because retrying cannot help.
            print(f"bad arguments: {error}", file=sys.stderr)
            return 2
        except IntemptApiError as error:
            print(f"API error: status={error.status} retryable={error.retryable}", file=sys.stderr)
            print(f"body: {error.body}", file=sys.stderr)
            return 1

    print(f"sent. look for user id {user_id!r} in Sources -> Live events.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
