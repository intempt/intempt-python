"""Test harness: a real HTTP server on loopback.

Deliberately not a mocking library. Mocking the transport would prove what the
SDK *intends* to send; a real socket proves what actually goes over the wire —
header framing, connection reuse, timeouts and JSON round-tripping. It also
keeps the package at zero test dependencies beyond pytest.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from intempt import BatchOptions, Intempt

ORG = "acme"
PROJECT = "web"
# A real 19-digit snowflake, past 2**53. Used to prove no numeric coercion.
SOURCE = "1841503112918048768"
API_KEY = "pfx0123456789abcdef.sec0123456789abcdef"


@dataclass
class Captured:
    method: str
    path: str
    headers: dict[str, str]
    body: Any
    #: Client-side port: the ground truth for connection reuse.
    socket_id: int


@dataclass
class Reply:
    status: int = 200
    body: str = "{}"
    headers: dict[str, str] = field(default_factory=dict)
    delay_s: float = 0.0


class RecordingServer:
    """Records requests and answers from a scripted queue."""

    def __init__(self) -> None:
        self.requests: list[Captured] = []
        self.replies: list[Reply] = []
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - required name
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8")
                try:
                    parsed: Any = json.loads(raw)
                except ValueError:
                    parsed = raw

                with outer._lock:
                    outer.requests.append(
                        Captured(
                            method=self.command,
                            path=self.path,
                            headers={k.lower(): v for k, v in self.headers.items()},
                            body=parsed,
                            socket_id=self.connection.getpeername()[1],
                        )
                    )
                    reply = outer.replies.pop(0) if outer.replies else Reply()

                if reply.delay_s:
                    import time as _t

                    _t.sleep(reply.delay_s)

                payload = reply.body.encode("utf-8")
                self.send_response(reply.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                for key, value in reply.headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_: Any) -> None:
                """Silence the default stderr access log."""

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self._server.server_address[1]}"

    def reset(self) -> None:
        with self._lock:
            self.requests.clear()
            self.replies.clear()

    def expect(self, *replies: Reply) -> None:
        with self._lock:
            self.replies.extend(replies)

    @property
    def bodies(self) -> list[Any]:
        return [r.body for r in self.requests]


@pytest.fixture(scope="session")
def server() -> Any:
    srv = RecordingServer()
    srv.start()
    yield srv
    srv.stop()


class RecordingLogger:
    """Captures log calls so tests can assert on what an operator would see."""

    def __init__(self) -> None:
        self.calls: dict[str, list[str]] = {
            "debug": [],
            "info": [],
            "warning": [],
            "error": [],
        }
        #: The `extra` mapping passed alongside each message, positionally
        #: matched to `calls`. It used to be swallowed, which meant a test could
        #: not tell "event dropped" from "event dropped, and here is which one" —
        #: and the structured half is the half an operator greps.
        self.context: dict[str, list[dict[str, Any]]] = {
            "debug": [],
            "info": [],
            "warning": [],
            "error": [],
        }

    def _record(self, level: str):
        def log(message: Any, *args: Any, **kwargs: Any) -> None:
            text = str(message)
            if args:
                try:
                    text = text % args
                except Exception:
                    text = f"{text} {args}"
            self.calls[level].append(text)
            extra = kwargs.get("extra")
            self.context[level].append(dict(extra) if isinstance(extra, dict) else {})

        return log

    def __getattr__(self, name: str):
        if name in ("debug", "info", "warning", "error"):
            return self._record(name)
        raise AttributeError(name)

    def has(self, level: str, needle: str) -> bool:
        return any(needle in line for line in self.calls[level])


@pytest.fixture
def logger() -> RecordingLogger:
    return RecordingLogger()


@pytest.fixture
def client(server: RecordingServer, logger: RecordingLogger):
    """Unbatched client pointed at the loopback server."""
    server.reset()
    made: list[Intempt] = []

    def build(**overrides: Any) -> Intempt:
        options: dict[str, Any] = {
            "org": ORG,
            "project": PROJECT,
            "api_key": API_KEY,
            "source_id": SOURCE,
            "host": server.host,
            "scheme": "http",
            "logger": logger,
        }
        options.update(overrides)
        instance = Intempt(**options)
        made.append(instance)
        return instance

    yield build

    for instance in made:
        # Teardown is best effort: a test that already closed the client, or one
        # that left it wedged on purpose, must not fail here instead of where it
        # actually asserted.
        with contextlib.suppress(Exception):
            instance.close()


@pytest.fixture
def batched(client):
    """Client with batching on and the timer effectively disabled."""

    def build(**overrides: Any) -> Intempt:
        batch = overrides.pop(
            "batch",
            BatchOptions(size=10, flush_ms=60_000, max_queue=1_000, flush_on_exit=False),
        )
        return client(batch=batch, **overrides)

    return build


@pytest.fixture(autouse=True)
def _quiet_logging():
    logging.getLogger("intempt").addHandler(logging.NullHandler())
