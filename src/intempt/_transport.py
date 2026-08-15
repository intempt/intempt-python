"""HTTP transport: one connection pool per client, credentials never logged.

Portions derived from mixpanel-python (Apache License 2.0), as recorded in
NOTICE: connection reuse across requests and the response error mapping follow
its Consumer.send. Changed to add a per-request timeout, to distinguish
retryable from non-retryable statuses, and to keep the credential out of every
error surface.

Copyright 2026 Intempt Technologies
Licensed under the Apache License, Version 2.0.
"""

from __future__ import annotations

import base64
import contextlib
import email.utils
import http.client
import json
import socket
import threading
import time
from typing import Any, NoReturn

from ._config import ResolvedConfig
from ._errors import IntemptApiError, IntemptConfigError

LIB_NAME = "intempt-python"
LIB_HEADER = "X-Intempt-Lib"


class ApiKeyCredentials:
    """Holds the key and hands out only the encoded header.

    The secret is stored on a private attribute and the class overrides every
    method that would otherwise print it. Python has no true private state, so
    this is a guardrail rather than a wall — but it stops the credential landing
    in a traceback, a log line or a debugger repr, which is where it actually
    leaks.
    """

    __slots__ = ("_header", "_prefix")

    def __init__(self, api_key: str) -> None:
        if not isinstance(api_key, str) or "." not in api_key:
            raise IntemptConfigError('api_key must be a public API key in "<prefix>.<secret>" form')
        prefix, _, secret = api_key.partition(".")
        if not prefix or not secret:
            raise IntemptConfigError('api_key must be a public API key in "<prefix>.<secret>" form')
        encoded = base64.b64encode(f"{prefix}:{secret}".encode()).decode()
        self._header = f"Basic {encoded}"
        self._prefix = prefix

    def authorization_header(self) -> str:
        return self._header

    def __repr__(self) -> str:
        return f"ApiKeyCredentials(prefix={self._prefix!r}, secret=<redacted>)"

    __str__ = __repr__

    def __reduce__(self) -> NoReturn:  # pragma: no cover - pickling a credential is a mistake
        raise TypeError("ApiKeyCredentials is not serialisable")


def parse_retry_after(value: str | None) -> int | None:
    """Seconds or an HTTP-date, in milliseconds. None when unusable.

    A negative or unparseable value yields None rather than a negative wait,
    which would mean no wait at all.
    """
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            # Raises on an unparseable value rather than returning None, which
            # an earlier version of this assumed. An unusable Retry-After must
            # be ignored, never propagated as a crash from an error path.
            parsed = email.utils.parsedate_to_datetime(value)
        except (ValueError, TypeError):
            return None
        if parsed is None:  # pragma: no cover - older interpreters
            return None
        delta_ms = int(parsed.timestamp() * 1000 - time.time() * 1000)
        return max(0, delta_ms)
    if seconds != seconds or seconds in (float("inf"), float("-inf")) or seconds < 0:
        return None
    return int(seconds * 1000)


class Transport:
    """Owns the connection. One instance per client."""

    def __init__(self, config: ResolvedConfig, credentials: ApiKeyCredentials) -> None:
        self._config = config
        self._credentials = credentials
        self._lock = threading.Lock()
        self._conn: http.client.HTTPConnection | None = None
        self._closed = False

    def set_config(self, config: ResolvedConfig) -> None:
        with self._lock:
            reconnect = (
                config.host != self._config.host
                or config.port != self._config.port
                or config.scheme != self._config.scheme
            )
            self._config = config
            if reconnect:
                self._drop_connection()

    def _drop_connection(self) -> None:
        if self._conn is not None:
            # Best effort: a socket the peer already dropped raises here, and
            # there is nothing useful to do about it while tearing down.
            with contextlib.suppress(Exception):  # pragma: no cover
                self._conn.close()
            self._conn = None

    def _connection(self) -> http.client.HTTPConnection:
        if self._conn is not None and self._config.keep_alive:
            return self._conn
        cls = (
            http.client.HTTPSConnection
            if self._config.scheme == "https"
            else http.client.HTTPConnection
        )
        conn = cls(self._config.host, self._config.port, timeout=self._config.timeout)
        if self._config.keep_alive:
            self._conn = conn
        return conn

    def post(self, path: str, body: Any) -> Any:
        """POST JSON, return the decoded body. Raises IntemptApiError on failure."""
        payload = json.dumps(body, default=_json_default).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
            "Authorization": self._credentials.authorization_header(),
            LIB_HEADER: f"{LIB_NAME}/{_version()}",
            "Connection": "keep-alive" if self._config.keep_alive else "close",
        }

        if self._config.debug:
            self._config.logger.debug("[intempt] POST %s", path)

        with self._lock:
            if self._closed:
                raise IntemptApiError("client is closed")
            try:
                conn = self._connection()
                conn.request("POST", path, body=payload, headers=headers)
                response = conn.getresponse()
                raw = response.read().decode("utf-8", errors="replace")
                status = response.status
                retry_after = parse_retry_after(response.getheader("Retry-After"))
            except socket.timeout as exc:
                self._drop_connection()
                raise IntemptApiError(
                    f"request timed out after {self._config.timeout}s", cause=exc
                ) from exc
            except (http.client.HTTPException, OSError) as exc:
                # A pooled connection the server already closed raises here on
                # the next use. Drop it so the retry gets a fresh socket rather
                # than failing again on the same dead one.
                self._drop_connection()
                raise IntemptApiError(str(exc) or type(exc).__name__, cause=exc) from exc

            if not self._config.keep_alive:
                self._drop_connection()

        if status < 200 or status >= 300:
            raise IntemptApiError(
                f"Intempt API responded {status}",
                status=status,
                body=raw,
                retry_after_ms=retry_after,
            )

        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            # A gateway can answer 200 with an HTML error page. That is a
            # successful HTTP exchange carrying a body we cannot parse, not a
            # failure to report.
            return raw

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._drop_connection()


def _json_default(value: Any) -> Any:
    import datetime as _dt

    if isinstance(value, _dt.datetime):
        return int(value.timestamp() * 1000)
    if isinstance(value, (set, frozenset)):
        return list(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serialisable")


def _version() -> str:
    from . import __version__

    return __version__
