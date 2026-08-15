"""The public client.

Copyright 2026 Intempt Technologies
Licensed under the Apache License, Version 2.0.
"""

from __future__ import annotations

import datetime as _dt
import uuid
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ._buffer import Buffer
from ._config import ResolvedConfig, merge_config, resolve_config
from ._errors import IntemptConfigError
from ._transport import Transport
from ._util import chunk, compact, ensure_timestamp, non_blank, require_identifier

#: Reserved event name the platform interprets as an identity write.
IDENTIFY_EVENT = "Identify"

#: Reserved names the platform recognises for commerce reporting. The only
#: reason this namespace exists is to encode them so callers cannot typo them.
COMMERCE_EVENTS = {
    "product_viewed": "Product viewed",
    "added_to_cart": "Added to cart",
    "ordered": "Product ordered",
}

_RESERVED = {IDENTIFY_EVENT.lower()}


class Consent:
    """Consent records. Timestamps here are epoch **seconds**, not milliseconds."""

    def __init__(self, client: Intempt) -> None:
        self._client = client

    def grant(self, **options: Any) -> None:
        self._record("accept", options)

    def revoke(self, **options: Any) -> None:
        self._record("reject", options)

    def _record(self, action: str, options: Mapping[str, Any]) -> None:
        name = "consent.grant" if action == "accept" else "consent.revoke"
        user_id = options.get("user_id")
        profile_id = options.get("profile_id")
        if not _present(user_id) and not _present(profile_id):
            raise IntemptConfigError(f"{name}: user_id must be a non-empty string")

        self._client._assert_open()
        if not self._client.is_opted_in():
            return

        config = self._client._config
        if profile_id and not config.source_id:
            raise IntemptConfigError(
                "consent: source_id must be configured to record consent by profile_id; "
                "pass user_id instead, or set source_id on the client"
            )

        raw = options.get("timestamp")
        millis = ensure_timestamp(raw) if raw is not None else _now_ms()
        body = compact(
            {
                "action": action,
                # Seconds, not milliseconds. The consent endpoint compares
                # timestamp * 1000 against millisecond bounds, so sending
                # milliseconds here puts the value far past 2040, where the
                # server silently replaces it with its own clock.
                "timestamp": millis // 1000,
                "userId": user_id,
                "profileId": profile_id,
                "category": options.get("category"),
                "validUntil": options.get("valid_until", "unlimited"),
                "email": options.get("email"),
                "message": options.get("message"),
                "reason": options.get("reason"),
                "method": options.get("method"),
                "deviceInfo": options.get("device_info"),
                "source": "Python tracker",
                # str(), never int(): a 19-digit snowflake loses precision.
                "sourceId": str(config.source_id) if profile_id and config.source_id else None,
            }
        )
        self._client._transport.post(config.project_path("/consents/data"), body)


class Ecommerce:
    """Commerce events, with the reserved names filled in."""

    def __init__(self, client: Intempt) -> None:
        self._client = client

    def product_viewed(self, *, product_id: str, **ids: Any) -> None:
        non_blank(product_id, "product_viewed", "product_id")
        require_identifier(ids, "product_viewed")
        self._client._track_lines(
            COMMERCE_EVENTS["product_viewed"], ids, [{"productId": product_id}]
        )

    def added_to_cart(self, *, product_id: str, quantity: int, **ids: Any) -> None:
        non_blank(product_id, "added_to_cart", "product_id")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            raise IntemptConfigError("added_to_cart: quantity must be a positive integer")
        require_identifier(ids, "added_to_cart")
        self._client._track_lines(
            COMMERCE_EVENTS["added_to_cart"],
            ids,
            [{"productId": product_id, "quantity": quantity}],
        )

    def ordered(self, *, products: Sequence[Mapping[str, Any]], **ids: Any) -> None:
        if not isinstance(products, Sequence) or isinstance(products, (str, bytes)) or not products:
            raise IntemptConfigError("ordered: products must be a non-empty sequence")
        lines = []
        for index, product in enumerate(products):
            product_id = product.get("product_id") if isinstance(product, Mapping) else None
            non_blank(product_id, f"ordered: products[{index}]", "product_id")
            quantity = product.get("quantity")
            if quantity is not None and (
                not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0
            ):
                raise IntemptConfigError(
                    f"ordered: products[{index}].quantity must be a positive integer"
                )
            lines.append(compact({"productId": product_id, "quantity": quantity}))
        require_identifier(ids, "ordered")
        self._client._track_lines(COMMERCE_EVENTS["ordered"], ids, lines)


class Intempt:
    """Server-side Intempt client. Data in, decisions out.

    Stateless by design: one instance is safe to share across threads and
    requests for every user, because every call carries its own identifier.
    """

    def __init__(self, **options: Any) -> None:
        self._config = resolve_config(**options)
        self._transport = Transport(self._config, self._config.credentials)
        self._opted_in = True
        self._closed = False

        self._buffer: Buffer | None = None
        if self._config.batch is not None:
            self._buffer = Buffer(
                options=self._config.batch,
                max_request_events=self._config.max_request_events,
                logger=self._config.logger,
                send=self._send,
            )

        self.consent = Consent(self)
        self.ecommerce = Ecommerce(self)

    # -- data in ----------------------------------------------------------

    def track(self, event: str, **options: Any) -> None:
        self._assert_event_name(event, "track")
        require_identifier(options, "track")
        self._submit([self._build_event(event, options)])

    def track_batch(self, events: Sequence[Mapping[str, Any]]) -> None:
        """Send many events, chunked so one oversized call is not one oversized request."""
        if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
            raise IntemptConfigError("track_batch: events must be a sequence")
        if not events:
            return

        wire = []
        for index, item in enumerate(events):
            if not isinstance(item, Mapping):
                raise IntemptConfigError(f"track_batch[{index}]: each event must be a mapping")
            name = self._assert_event_name(item.get("event"), f"track_batch[{index}]")
            require_identifier(item, f"track_batch[{index}]")
            rest = {k: v for k, v in item.items() if k != "event"}
            wire.append(self._build_event(name, rest))

        if self._buffer is not None or not self.is_opted_in():
            self._submit(wire)
            return

        groups = chunk(wire, self._config.max_request_events)
        if self._config.max_concurrent_requests <= 1:
            for group in groups:
                self._send(group)
            return

        # Bounded parallelism. Errors are re-raised after every worker settles,
        # so a failure cannot leave siblings running unobserved.
        workers = min(self._config.max_concurrent_requests, len(groups))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(self._send, group) for group in groups]
            errors = [f.exception() for f in futures]
        for error in errors:
            if error is not None:
                raise error

    def identify(self, **options: Any) -> None:
        require_identifier(options, "identify")
        event = options.pop("event", None)
        traits = options.pop("traits", None)
        self._submit(
            [
                self._build_event(
                    self._reserved_name(event, "identify"),
                    {**options, "user_attributes": traits},
                )
            ]
        )

    def group(self, *, account_id: str, **options: Any) -> None:
        non_blank(account_id, "group", "account_id")
        event = options.pop("event", None)
        attributes = options.pop("attributes", None)
        self._submit(
            [
                self._build_event(
                    self._reserved_name(event, "group"),
                    {**options, "account_id": account_id, "account_attributes": attributes},
                )
            ]
        )

    def alias(self, *, user_id: str, previous_user_id: str, **options: Any) -> None:
        non_blank(user_id, "alias", "user_id")
        non_blank(previous_user_id, "alias", "previous_user_id")
        event = options.pop("event", None)
        item = self._build_event(
            self._reserved_name(event, "alias"), {**options, "user_id": user_id}
        )
        item["payload"][0]["anotherUserId"] = previous_user_id
        self._submit([item])

    # -- decisions out ----------------------------------------------------

    def recommend(
        self,
        *,
        feed_id: str,
        fields: Sequence[str],
        user_id: str | None = None,
        account_id: str | None = None,
        limit: int | None = None,
        product_id: str | None = None,
    ) -> Any:
        """Product recommendations from a feed.

        Experiments and personalizations are deliberately absent: they resolve a
        web experience against a page, and a server has no page.
        """
        self._assert_open()
        non_blank(feed_id, "recommend", "feed_id")
        if not isinstance(fields, Sequence) or isinstance(fields, (str, bytes)) or not fields:
            raise IntemptConfigError("recommend: fields must be a non-empty sequence")
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
        ):
            raise IntemptConfigError("recommend: limit must be a positive integer")

        # The feeds API resolves a single {id, type} entity, so exactly one.
        if _present(user_id) and _present(account_id):
            raise IntemptConfigError(
                "recommend: pass user_id or account_id, not both — the feeds API "
                "resolves a single entity"
            )
        if _present(user_id):
            identity = {"id": user_id, "type": "user"}
        elif _present(account_id):
            identity = {"id": account_id, "type": "account"}
        else:
            raise IntemptConfigError("recommend: one of user_id or account_id is required")

        body = compact(
            {
                **identity,
                "fields": list(fields),
                "limit": limit,
                "productId": product_id,
                "sourceId": str(self._config.source_id) if self._config.source_id else None,
            }
        )
        return self._transport.post(self._config.project_path(f"/feeds/{feed_id}/data"), body)

    # -- privacy ----------------------------------------------------------

    def opt_in(self) -> None:
        self._opted_in = True

    def opt_out(self) -> None:
        """Suppress all outbound writes: track, batch, commerce and consent.

        ``recommend()`` is unaffected — it sends an identifier the caller already
        holds and returns a decision rather than storing anything.
        """
        self._opted_in = False

    def is_opted_in(self) -> bool:
        return self._opted_in and not self._closed

    # -- config -----------------------------------------------------------

    def set_config(self, **patch: Any) -> None:
        self._config = merge_config(self._config, patch)
        self._transport.set_config(self._config)

    @property
    def config(self) -> ResolvedConfig:
        """A frozen snapshot. Mutating it cannot change the client."""
        return self._config

    @property
    def buffered(self) -> int:
        return self._buffer.size if self._buffer else 0

    # -- lifecycle --------------------------------------------------------

    def flush(self) -> None:
        """Drain the buffer. A no-op when batching is off."""
        if self._buffer is not None:
            self._buffer.flush()

    def close(self) -> None:
        """Flush, then release the connection. The client is unusable after."""
        if self._closed:
            return
        if self._buffer is not None:
            self._buffer.close()
        self._closed = True
        self._transport.close()

    def __enter__(self) -> Intempt:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- internals --------------------------------------------------------

    def _assert_open(self) -> None:
        if self._closed:
            raise IntemptConfigError(
                "Intempt client is closed. Calls after close() are not sent; create a new client."
            )

    def _assert_event_name(self, event: Any, method: str) -> str:
        """Validate and return the name.

        Returns rather than asserting so the caller gets a narrowed `str`. A
        validator that only raises leaves `Any | None` flowing into a `str`
        parameter, which type checking catches and readers do not.
        """
        if not isinstance(event, str) or not event.strip():
            raise IntemptConfigError(f"{method}: event name is required")
        if event.strip().lower() in _RESERVED:
            raise IntemptConfigError(
                f'{method}: "{event}" is reserved; use identify(), group() or alias()'
            )
        return event

    @staticmethod
    def _reserved_name(event: Any, method: str) -> str:
        if event is None:
            return IDENTIFY_EVENT
        return non_blank(event, method, "event")

    def _track_path(self) -> str:
        source_id = self._config.source_id
        if source_id:
            from urllib.parse import quote

            return self._config.project_path(f"/sources/{quote(str(source_id), safe='')}/track")
        return self._config.project_path("/track")

    def _build_event(self, name: str, options: Mapping[str, Any]) -> dict[str, Any]:
        raw = options.get("timestamp")
        item = compact(
            {
                "eventId": str(uuid.uuid4()),
                "timestamp": ensure_timestamp(raw) if raw is not None else _now_ms(),
                "profileId": options.get("profile_id"),
                "userId": options.get("user_id"),
                "accountId": options.get("account_id"),
                "data": options.get("properties"),
                "userAttributes": options.get("user_attributes"),
                "accountAttributes": options.get("account_attributes"),
            }
        )
        return {"name": name, "payload": [item]}

    def _track_lines(
        self, name: str, ids: Mapping[str, Any], lines: Sequence[Mapping[str, Any]]
    ) -> None:
        """One event carrying several payload items, one per line.

        Kept bit-compatible with the 1.x commerce wire format: the lines share a
        single eventId. Nothing on the ingestion path dedupes on eventId, so this
        cannot collapse rows.
        """
        event_id = str(uuid.uuid4())
        raw = ids.get("timestamp")
        millis = ensure_timestamp(raw) if raw is not None else _now_ms()
        payload = [
            compact(
                {
                    "eventId": event_id,
                    "timestamp": millis,
                    "profileId": ids.get("profile_id"),
                    "userId": ids.get("user_id"),
                    "accountId": ids.get("account_id"),
                    "data": dict(line),
                }
            )
            for line in lines
        ]
        self._submit([{"name": name, "payload": payload}])

    def _submit(self, events: list[dict[str, Any]]) -> None:
        # A closed client raises; an opted-out client returns quietly. Silently
        # discarding a write after close is how events get lost without anyone
        # being told.
        self._assert_open()
        if not self.is_opted_in() or not events:
            return
        if self._buffer is not None:
            for event in events:
                self._buffer.enqueue(event)
            return
        self._send(events)

    def _send(self, events: list[dict[str, Any]]) -> None:
        """Post one request. Also the buffer's send callback.

        The opt-out gate is repeated here because the buffer calls this directly.
        Without it, events captured before opt_out() are still transmitted by a
        later flush(), close() or the exit hook.
        """
        if not self.is_opted_in():
            self._config.logger.warning(
                "[intempt] opted out; discarding %d buffered event(s) rather than sending",
                len(events),
            )
            return
        self._transport.post(self._track_path(), {"track": events})


def _present(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _now_ms() -> int:
    return int(_dt.datetime.now(_dt.timezone.utc).timestamp() * 1000)
