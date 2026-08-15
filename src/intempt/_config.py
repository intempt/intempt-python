"""Configuration resolution.

Copyright 2026 Intempt Technologies
Licensed under the Apache License, Version 2.0.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from ._errors import IntemptConfigError
from ._util import assert_logger

DEFAULT_HOST = "api.intempt.com"
DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_REQUEST_EVENTS = 50
DEFAULT_MAX_CONCURRENT_REQUESTS = 1

#: Options fixed at construction because the connection pool is built once.
_FIXED = ("org", "project", "api_key", "source_id", "batch", "keep_alive")


@dataclass(frozen=True)
class BatchOptions:
    """Buffering behaviour. ``None`` for ``batch`` disables buffering entirely."""

    size: int = 50
    flush_ms: int = 5_000
    max_queue: int = 10_000
    flush_on_exit: bool = True

    def __post_init__(self) -> None:
        if self.size < 1:
            raise IntemptConfigError("batch.size must be at least 1")
        if self.flush_ms < 1:
            raise IntemptConfigError("batch.flush_ms must be at least 1")
        if self.max_queue < self.size:
            raise IntemptConfigError("batch.max_queue must be at least batch.size")


@dataclass(frozen=True)
class ResolvedConfig:
    org: str
    project: str
    #: The credential, never the raw key.
    #:
    #: This used to be ``api_key: str``, which meant ``repr(client.config)`` —
    #: and anything that logs a config, including most exception reporters —
    #: printed the secret in full. Holding only the encoded form removes the one
    #: leak the SDK controls. The same defect appeared independently in the PHP
    #: SDK, which is why all three now carry a guard test for it.
    credentials: Any
    host: str = DEFAULT_HOST
    port: int | None = None
    scheme: str = "https"
    path: str = ""
    timeout: float = DEFAULT_TIMEOUT
    keep_alive: bool = True
    debug: bool = False
    source_id: str | None = None
    batch: BatchOptions | None = None
    max_request_events: int = DEFAULT_MAX_REQUEST_EVENTS
    max_concurrent_requests: int = DEFAULT_MAX_CONCURRENT_REQUESTS
    logger: Any = field(default_factory=lambda: logging.getLogger("intempt"))

    def project_path(self, suffix: str) -> str:
        from urllib.parse import quote

        org = quote(self.org, safe="")
        project = quote(self.project, safe="")
        return f"{self.path}/v1/{org}/projects/{project}{suffix}"


def _split_host(host: str) -> tuple[str, int | None]:
    """Accept ``host`` or ``host:port``.

    The port is validated rather than assumed: an unparseable or zero port would
    otherwise reach the socket layer and fail with something unrecognisable.
    """
    if not isinstance(host, str) or not host.strip():
        raise IntemptConfigError("host must not be empty")
    hostname, _, raw_port = host.strip().partition(":")
    if not hostname:
        raise IntemptConfigError("host must not be empty")
    if not raw_port:
        return hostname, None
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise IntemptConfigError(f"invalid port in host: {host}") from exc
    if port <= 0 or port > 65535:
        raise IntemptConfigError(f"invalid port in host: {host}")
    return hostname, port


def resolve_config(**options: Any) -> ResolvedConfig:
    """Validate and normalise constructor options."""
    for name in ("org", "project", "api_key"):
        value = options.get(name)
        if not isinstance(value, str) or not value.strip():
            raise IntemptConfigError(f'Intempt: "{name}" is required')

    source_id = options.get("source_id")
    if source_id is not None:
        # str(), never int(). A 19-digit snowflake exceeds float precision and a
        # numeric round trip silently addresses a different source.
        source_id = str(source_id)
        if not source_id.strip():
            raise IntemptConfigError('Intempt: "source_id" must not be empty when provided')

    # None means "not supplied, use the default". An explicit empty string is a
    # mistake and is refused rather than quietly becoming the default.
    raw_host = options.get("host")
    host, port = _split_host(DEFAULT_HOST if raw_host is None else raw_host)

    scheme = options.get("scheme", "https")
    if scheme not in ("http", "https"):
        raise IntemptConfigError(f'unsupported scheme "{scheme}"')

    timeout = options.get("timeout", DEFAULT_TIMEOUT)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise IntemptConfigError("timeout must be a positive number of seconds")

    max_request_events = options.get("max_request_events", DEFAULT_MAX_REQUEST_EVENTS)
    if not isinstance(max_request_events, int) or max_request_events < 1:
        raise IntemptConfigError("max_request_events must be a positive integer")

    max_concurrent = options.get("max_concurrent_requests", DEFAULT_MAX_CONCURRENT_REQUESTS)
    if not isinstance(max_concurrent, int) or max_concurrent < 1:
        raise IntemptConfigError("max_concurrent_requests must be a positive integer")

    batch = options.get("batch")
    if batch is not None and not isinstance(batch, BatchOptions):
        if not isinstance(batch, Mapping):
            raise IntemptConfigError("batch must be a BatchOptions or a mapping")
        batch = BatchOptions(**dict(batch))

    logger = options.get("logger") or logging.getLogger("intempt")
    assert_logger(logger)

    # Imported here rather than at module scope: _transport imports _config for
    # ResolvedConfig, so a top-level import would be circular.
    from ._transport import ApiKeyCredentials

    return ResolvedConfig(
        org=options["org"],
        project=options["project"],
        credentials=ApiKeyCredentials(options["api_key"]),
        host=host,
        port=port,
        scheme=scheme,
        path=options.get("path", ""),
        timeout=float(timeout),
        keep_alive=bool(options.get("keep_alive", True)),
        debug=bool(options.get("debug", False)),
        source_id=source_id,
        batch=batch,
        max_request_events=max_request_events,
        max_concurrent_requests=max_concurrent,
        logger=logger,
    )


def merge_config(current: ResolvedConfig, patch: Mapping[str, Any]) -> ResolvedConfig:
    """Apply a patch to a live client's config.

    The fixed options are refused rather than ignored. Accepting them silently
    left a caller believing they had changed something that never moved.
    """
    # Subtract the fixed names before the unknown check, so a caller passing
    # api_key gets "is fixed at construction" rather than "unknown option".
    # api_key stopped being a ResolvedConfig field when the raw key was removed,
    # which silently demoted it to the vaguer error.
    known = {f.name for f in ResolvedConfig.__dataclass_fields__.values()}
    unknown = set(patch) - known - {"host"} - set(_FIXED)
    if unknown:
        raise IntemptConfigError("set_config: unknown option(s): " + ", ".join(sorted(unknown)))

    for name in _FIXED:
        if name in patch:
            raise IntemptConfigError(
                f'set_config: "{name}" is fixed at construction because the connection '
                "pool is built once. Pass it to Intempt() instead."
            )

    changes: dict[str, Any] = {}
    if "host" in patch:
        host, port = _split_host(patch["host"])
        changes["host"] = host
        # A new host with no port must clear the old one, or the next request
        # goes to new-host:old-port, a pair the caller never asked for.
        changes["port"] = port
    if "scheme" in patch:
        if patch["scheme"] not in ("http", "https"):
            raise IntemptConfigError(f'unsupported scheme "{patch["scheme"]}"')
        changes["scheme"] = patch["scheme"]
    if "timeout" in patch:
        timeout = patch["timeout"]
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise IntemptConfigError("timeout must be a positive number of seconds")
        changes["timeout"] = float(timeout)
    if "logger" in patch:
        assert_logger(patch["logger"])
        changes["logger"] = patch["logger"]
    for name in ("path", "debug", "max_request_events", "max_concurrent_requests"):
        if name in patch:
            changes[name] = patch[name]

    return replace(current, **changes)
