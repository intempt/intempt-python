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
from ._flags import IDENTITY_REQUIREMENT, KEY_PATTERN, UNANSWERED, FlagContext, FlagDetail
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

    # -- flags ------------------------------------------------------------

    def variation(self, key: str, context: FlagContext, default_value: Any) -> Any:
        """The value assigned to this person for ``key``, or ``default_value``.

        Ask for a key, never a mode. Whether the key names an experiment, a personalization or a
        flag is the platform's business.
        """
        return self._variation_detail(key, context, default_value).value

    def _variation_detail(self, key: str, context: FlagContext, default_value: Any) -> FlagDetail:
        """Internal. NOT public, deliberately.

        It returns a ``reason``, and the platform does not send one: a held-back person's
        experience is absent from the evaluation response entirely rather than present with a
        cause. So every reason would read ``off`` — including for someone who WAS targeted and
        did receive the variant. That is a wrong answer, not a missing one, and a method whose
        only job is explaining why must not guess.

        :meth:`variation` uses it for the value, which is correct either way. It becomes public
        when the serving contract carries a reason.
        """
        self._assert_open()
        self._assert_flag_key(key, "variation")
        self._assert_flag_context(context, "variation")

        choices = self._choose_or_empty(context, [key])
        for choice in choices:
            if choice.get("name") == key:
                body = choice.get("body")
                return FlagDetail(
                    value=default_value if body is None else body,
                    reason=choice.get("reason") or UNANSWERED,
                )
        return FlagDetail(value=default_value, reason=UNANSWERED)

    def all_flags(self, context: FlagContext) -> dict[str, Any]:
        """Every key assigned to this person, in one call.

        A key the service answered without a body is OMITTED rather than mapped to ``None``.
        ``variation`` treats a null body as "no value was served" and hands back the caller's
        default; a ``None`` here would make the two public read paths disagree about the identical
        wire response, and would read as "assigned nothing" when it means "not assigned".
        """
        self._assert_open()
        self._assert_flag_context(context, "all_flags")
        out: dict[str, Any] = {}
        for choice in self._choose_or_empty(context, None):
            name = choice.get("name")
            body = choice.get("body")
            if name and body is not None:
                out[name] = body
        return out

    def bool_variation(self, key: str, context: FlagContext, default_value: bool) -> bool:
        value = self.variation(key, context, default_value)
        # A served value of the wrong type is a misconfiguration, not something to coerce:
        # bool("false") is True, and a silent coercion is indistinguishable from a real answer.
        return value if isinstance(value, bool) else default_value

    def string_variation(self, key: str, context: FlagContext, default_value: str) -> str:
        value = self.variation(key, context, default_value)
        return value if isinstance(value, str) else default_value

    def number_variation(self, key: str, context: FlagContext, default_value: float) -> float:
        value = self.variation(key, context, default_value)
        # bool is a subclass of int in Python, so True would otherwise pass as the number 1.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default_value
        # Explicit: variation() is typed Any, so mypy cannot narrow through the
        # negated isinstance above and reports a bare `return value` as
        # no-any-return against the declared float.
        return float(value)

    def json_variation(
        self, key: str, context: FlagContext, default_value: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        value = self.variation(key, context, default_value)
        return value if isinstance(value, Mapping) else default_value

    def wait_for_initialization(self, timeout_ms: int | None = None) -> None:
        """Returns immediately. ``timeout_ms`` is ACCEPTED AND IGNORED.

        Present so the cross-SDK surface is the same everywhere, and so a caller porting from an
        SDK that polls a local flag store does not have to remove the call. Evaluation here is
        remote: each ``variation()`` is a request, so there is no local state to wait for and
        nothing a timeout could bound. It is named in the signature rather than dropped so the
        port compiles; it is discarded below rather than left unread so that "inert" is a
        property of the code and not only of this docstring.
        """
        self._assert_open()
        del timeout_ms

    def _assert_flag_key(self, key: str, method: str) -> None:
        """A key the service will refuse is a caller mistake, so it raises here.

        ``non_blank`` is not enough: ``ExperienceApiChooseRequest`` validates every name against
        ``^[a-zA-Z0-9_-]*$``, so ``pricing.cta`` or ``checkout v2`` is answered with a 400. A 400
        reaches :meth:`_choose_or_empty` as a transport failure and is absorbed into the caller's
        default — permanently, and identically to a key that was never created. Failing at the
        call site is the difference between a typo found in development and a flag that is dead
        in production.
        """
        non_blank(key, method, "key")
        if not KEY_PATTERN.match(key):
            raise IntemptConfigError(
                f"{method}: key must match {KEY_PATTERN.pattern} "
                f"(letters, digits, underscore, hyphen); got {key!r}"
            )

    def _assert_flag_context(self, context: FlagContext, method: str) -> None:
        """A context the service cannot answer is a caller mistake, so it raises here.

        ``buildAudienceRequest`` raises on an identity that is neither PROFILE nor USER. That
        surfaces as a 5xx, which :meth:`_choose_or_empty` absorbs into the caller's default with
        one warning line — so a client constructed without a ``source_id`` and read with only a
        ``profile_id`` would serve defaults forever and look like a working integration. Every
        other identified call in this SDK validates its identity up front; this is the same rule.
        """
        if context is None or not isinstance(context, FlagContext):
            raise IntemptConfigError(f"{method}: context must be a FlagContext")
        if not context.has_identity(self._config.source_id):
            raise IntemptConfigError(f"{method}: {IDENTITY_REQUIREMENT}")

    def _choose_or_empty(
        self, context: FlagContext, names: list[str] | None
    ) -> list[Mapping[str, Any]]:
        """A transport failure returns no choices rather than raising.

        This is the entire reason ``default_value`` is required: a network failure, a 5xx or a
        timeout must resolve to the value the caller chose. A flag SDK that raises when the
        service is unreachable takes the application down with it, which is the opposite of what a
        kill switch is for. A validation mistake still raises, because that is a programming error
        the caller can fix rather than a runtime condition to absorb.
        """
        body = compact(
            {
                "identification": compact(
                    {
                        "userId": context.user_id,
                        "profileId": context.profile_id,
                        "sourceId": str(self._config.source_id) if self._config.source_id else None,
                    }
                ),
                "names": names,
                # Load-bearing, not decoration. `ExperienceRequest.getDevice()` turns a null
                # device into the SQL predicate "0", which is false for every row -- omit this
                # and EVERY evaluation returns zero experiences.
                "device": "all",
                # Scopes the exposure and gates a `once` / `once_per_visit` display. Absent, the
                # service stores and compares a single shared placeholder session.
                "sessionId": context.session_id,
            }
        )
        try:
            response = self._transport.post(
                self._config.project_path("/optimization/choose-api"), body
            )
        # Any transport failure must yield the caller's default. (Deliberately broad; note that
        # ruff's `select` here carries no BLE rule, so a `noqa: BLE001` would suppress nothing
        # and only look like a guard.)
        except Exception:
            self._config.logger.warning("[intempt] flag evaluation failed, using defaults")
            return []
        if not isinstance(response, Mapping):
            return []
        choices = response.get("choices")
        return list(choices) if isinstance(choices, list) else []

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
            raise IntemptConfigError(f'{method}: "{event}" is reserved; use identify() or group()')
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
