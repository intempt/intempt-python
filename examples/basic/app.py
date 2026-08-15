"""A small Flask-free sample: one HTTP server that instruments itself.

Run it against a real project:

    export INTEMPT_ORG=my-org
    export INTEMPT_PROJECT=my-project
    export INTEMPT_API_KEY='prefix.secret'
    export INTEMPT_SOURCE_ID=684508596718616576
    export INTEMPT_FEED_ID=5292          # optional, enables /recommend
    python examples/basic/app.py

Then, in another shell:

    curl -X POST localhost:8080/signup   -d 'user=ada@example.com'
    curl -X POST localhost:8080/purchase -d 'user=ada@example.com&sku=21&qty=2'
    curl        'localhost:8080/recommend?user=ada@example.com'
    curl -X POST localhost:8080/forget   -d 'user=ada@example.com'

The point of the sample is the shape, not the routes: one client for the whole
process, an identifier on every call, batching on, and a shutdown that drains.

Copyright 2026 Intempt Technologies
Licensed under the Apache License, Version 2.0.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from intempt import BatchOptions, Intempt, IntemptApiError

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("sample")


def build_client() -> Intempt:
    missing = [
        name
        for name in ("INTEMPT_ORG", "INTEMPT_PROJECT", "INTEMPT_API_KEY")
        if not os.environ.get(name)
    ]
    if missing:
        sys.exit("missing environment: " + ", ".join(missing))

    # One client for the whole process. It holds no per-user state, so sharing it
    # across every request and thread is the intended use, not a shortcut.
    #
    # host and scheme are read from the environment so this sample can be pointed
    # at a local server. A sample you cannot point somewhere else is a sample
    # nobody can test, including its author — the first version of this file
    # hardcoded both and had never been run.
    return Intempt(
        org=os.environ["INTEMPT_ORG"],
        project=os.environ["INTEMPT_PROJECT"],
        api_key=os.environ["INTEMPT_API_KEY"],
        source_id=os.environ.get("INTEMPT_SOURCE_ID"),
        host=os.environ.get("INTEMPT_HOST") or "api.intempt.com",
        scheme=os.environ.get("INTEMPT_SCHEME") or "https",
        # Batching is right for a long-lived server: it trades a little latency
        # for far fewer requests. Leave it off in Lambda, where the process can
        # vanish before a flush.
        batch=BatchOptions(size=20, flush_ms=2_000, max_queue=5_000),
        logger=log,
    )


intempt = build_client()
FEED_ID = os.environ.get("INTEMPT_FEED_ID")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _body(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode() if length else ""
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def _reply(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        form = self._body()
        user = form.get("user")
        if not user:
            return self._reply(400, {"error": "user is required"})

        try:
            if route == "/signup":
                # identify() writes traits. The platform resolves identity from
                # user_id itself, so there is no id to mint here.
                intempt.identify(user_id=user, traits={"plan": form.get("plan", "free")})
                intempt.track("signed_up", user_id=user)
                return self._reply(201, {"ok": True})

            if route == "/purchase":
                sku = form.get("sku")
                if not sku:
                    return self._reply(400, {"error": "sku is required"})
                quantity = int(form.get("qty", "1"))
                intempt.ecommerce.ordered(
                    user_id=user, products=[{"product_id": sku, "quantity": quantity}]
                )
                return self._reply(201, {"ok": True})

            if route == "/forget":
                # Revoking consent is a write like any other, and it is gated by
                # opt-out the same way.
                intempt.consent.revoke(user_id=user, reason="user requested deletion")
                return self._reply(202, {"ok": True})

            return self._reply(404, {"error": "no such route"})

        except IntemptApiError as exc:
            # Every method raises. Nothing is swallowed, so a real app decides
            # here whether the failure should reach the customer.
            log.error("intempt rejected the write: %s", exc)
            return self._reply(502, {"error": "analytics write failed"})
        except ValueError as exc:
            return self._reply(400, {"error": str(exc)})

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/recommend":
            return self._reply(404, {"error": "no such route"})
        if not FEED_ID:
            return self._reply(503, {"error": "set INTEMPT_FEED_ID to enable this route"})

        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        user = query.get("user")
        if not user:
            return self._reply(400, {"error": "user is required"})

        try:
            feed = intempt.recommend(user_id=user, feed_id=FEED_ID, fields=["id", "title"], limit=5)
        except IntemptApiError as exc:
            log.error("feed lookup failed: %s", exc)
            # A recommendation is an enhancement. Degrade rather than fail the
            # page: an empty list is a worse experience, an error is a broken one.
            return self._reply(200, {"items": []})

        return self._reply(200, feed or {"items": []})

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s", fmt % args)


def main() -> None:
    port = int(os.environ.get("SAMPLE_PORT") or "8080")
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)

    def shutdown(*_) -> None:
        # shutdown() blocks until serve_forever() returns, and a signal handler
        # runs on the thread that is *inside* serve_forever(). Calling it here
        # directly deadlocks: the handler waits for a loop that cannot proceed
        # until the handler returns. Hand it to another thread instead.
        #
        # Found by actually running this sample under SIGTERM. It looked correct
        # and hung for 15 seconds every time.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log.info("listening on http://127.0.0.1:%d", port)
    try:
        server.serve_forever()
    finally:
        # The drain belongs here, not in the signal handler: this runs on the
        # normal exit path too, so a plain return still flushes.
        log.info("draining %d buffered event(s)…", intempt.buffered)
        # close() drains for up to 30s and says what it could not send. Without
        # it, everything still in the buffer is lost on exit.
        intempt.close()


if __name__ == "__main__":
    main()
