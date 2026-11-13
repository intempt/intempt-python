"""The public client.

Copyright 2026 Intempt Technologies
Licensed under the Apache License, Version 2.0.
"""

from __future__ import annotations

import datetime as _dt
import socket
import time
import uuid
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ._buffer import Buffer
from ._config import ResolvedConfig, merge_config, resolve_config
from ._delivery import DeliveryFailure, FlushResult, deliver, event_ids
from ._errors import IntemptApiError, IntemptConfigError
from ._flags import (
    IDENTITY_REQUIREMENT,
    KEY_PATTERN,
    UNANSWERED,
    FlagContext,
    FlagDetail,
    FlagError,
)
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

    def grant(self, **options: Any) -> bool:
        """True when the server accepted the record. A failure is reported, not raised."""
        return self._record("accept", options)

    def revoke(self, **options: Any) -> bool:
        """True when the server accepted the record. A failure is reported, not raised."""
        return self._record("reject", options)

    def _record(self, action: str, options: Mapping[str, Any]) -> bool:
        name = "consent.grant" if action == "accept" else "consent.revoke"
        user_id = options.get("user_id")
        profile_id = options.get("profile_id")
        if not _present(user_id) and not _present(profile_id):
            raise IntemptConfigError(f"{name}: user_id must be a non-empty string")

        # Deliberately NOT gated on opt-out (ruling D-5, INT-7437): a withdrawal must reach the
        # server precisely when the person has opted out. intempt-js consent() does the same.
        self._client._assert_open()

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
        path = config.project_path("/consents/data")
        transport = self._client._transport

        def send() -> None:
            # Returns nothing on purpose: deliver() reads a False return as a deliberate
            # discard, and a 2xx body of `false` must not turn an accepted record into a
            # silent failure (R2).
            transport.post(path, body)

        return self._client._deliver_now(send, 1, (), label=name)


class Ecommerce:
    """Commerce events, with the reserved names filled in."""

    def __init__(self, client: Intempt) -> None:
        self._client = client

    def product_viewed(self, *, product_id: str, **ids: Any) -> bool:
        non_blank(product_id, "product_viewed", "product_id")
        require_identifier(ids, "product_viewed")
        return self._client._track_lines(
            COMMERCE_EVENTS["product_viewed"], ids, [{"productId": product_id}]
        )

    def added_to_cart(self, *, product_id: str, quantity: int, **ids: Any) -> bool:
        non_blank(product_id, "added_to_cart", "product_id")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            raise IntemptConfigError("added_to_cart: quantity must be a positive integer")
        require_identifier(ids, "added_to_cart")
        return self._client._track_lines(
            COMMERCE_EVENTS["added_to_cart"],
            ids,
            [{"productId": product_id, "quantity": quantity}],
        )

    def ordered(self, *, products: Sequence[Mapping[str, Any]], **ids: Any) -> bool:
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
        return self._client._track_lines(COMMERCE_EVENTS["ordered"], ids, lines)


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
        #: Injectable so a test can assert the resend wait without taking it.
        self._sleep = time.sleep
        #: 401/403 on a flag call is logged at error once per instance, then at debug.
        self._flag_auth_logged = False

        self._buffer: Buffer | None = None
        if self._config.batch is not None:
            self._buffer = Buffer(
                options=self._config.batch,
                max_request_events=self._config.max_request_events,
                logger=self._config.logger,
                send=self._send,
                report=self._report_delivery_failure,
            )

        self.consent = Consent(self)
        self.ecommerce = Ecommerce(self)

    # -- data in ----------------------------------------------------------

    def track(self, event: str, **options: Any) -> bool:
        """Queue (batch mode) or send (direct mode) one event.

        Returns whether it was accepted: queued, or delivered when batching is off. A delivery
        problem never raises (INT-7444); it reaches ``on_delivery_failure``. A bad argument or a
        closed client still raises, because those are programming errors.
        """
        self._assert_event_name(event, "track")
        require_identifier(options, "track")
        return self._submit([self._build_event(event, options)])

    def track_batch(self, events: Sequence[Mapping[str, Any]]) -> bool:
        """Send many events, chunked so one oversized call is not one oversized request.

        True when every event was accepted; see :meth:`track`.
        """
        if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
            raise IntemptConfigError("track_batch: events must be a sequence")
        if not events:
            return True

        wire = []
        for index, item in enumerate(events):
            if not isinstance(item, Mapping):
                raise IntemptConfigError(f"track_batch[{index}]: each event must be a mapping")
            name = self._assert_event_name(item.get("event"), f"track_batch[{index}]")
            require_identifier(item, f"track_batch[{index}]")
            rest = {k: v for k, v in item.items() if k != "event"}
            wire.append(self._build_event(name, rest))

        if self._buffer is not None or not self.is_opted_in():
            return self._submit(wire)

        groups = chunk(wire, self._config.max_request_events)
        if self._config.max_concurrent_requests <= 1:
            return all([self._send_now(group) for group in groups])

        workers = min(self._config.max_concurrent_requests, len(groups))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(self._send_now, groups))
        return all(results)

    def identify(self, **options: Any) -> bool:
        require_identifier(options, "identify")
        event = options.pop("event", None)
        traits = options.pop("traits", None)
        return self._submit(
            [
                self._build_event(
                    self._reserved_name(event, "identify"),
                    {**options, "user_attributes": traits},
                )
            ]
        )

    def group(self, *, account_id: str, **options: Any) -> bool:
        non_blank(account_id, "group", "account_id")
        event = options.pop("event", None)
        attributes = options.pop("attributes", None)
        return self._submit(
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

    def variation(
        self, key: str, context: FlagContext, default_value: Any, *, disable_exposure: bool = False
    ) -> Any:
        """The value assigned to this person for ``key``, or ``default_value``.

        Ask for a key, never a mode. Whether the key names an experiment, a personalization or a
        flag is the platform's business. Reading a key records the exposure; pass
        ``disable_exposure=True`` for a read that does not show the value (pre-render, prefetch).
        """
        return self.variation_detail(
            key, context, default_value, disable_exposure=disable_exposure
        ).value

    def variation_detail(
        self, key: str, context: FlagContext, default_value: Any, *, disable_exposure: bool = False
    ) -> FlagDetail:
        """The value plus why (INT-7454).

        ``reason`` is ``error`` with :attr:`FlagDetail.error` set when the evaluation failed and
        the default was returned. Otherwise it is ``off`` whether or not a value was served: the
        platform sends no reason, and a held-back person's experience is simply absent from the
        response, so this method does not guess between "off" and "not targeted" (INT-7457).
        """
        self._assert_open()
        self._assert_flag_key(key, "variation")
        self._assert_flag_context(context, "variation")

        choices, error = self._choose(context, [key], key=key, exposure=not disable_exposure)
        if error is not None:
            return FlagDetail(value=default_value, reason="error", error=error)
        for choice in choices:
            if choice.get("name") == key:
                body = choice.get("body")
                return FlagDetail(
                    value=default_value if body is None else body,
                    # Not read off the wire (INT-7457): ExperienceApiChoose carries only
                    # name/group/body, so a "reason" key could only come from a fixture.
                    reason=UNANSWERED,
                )
        return FlagDetail(value=default_value, reason=UNANSWERED)

    def all_flags(self, context: FlagContext, *, log_exposure: bool = False) -> dict[str, Any]:
        """Every key assigned to this person, in one call.

        Evaluates without recording exposures (INT-7456): a bulk read is not a display, and
        counting every key as seen inflated exposures for variants nobody rendered. Read the key
        you show with :meth:`variation`, or pass ``log_exposure=True`` to record them all.

        A key the service answered without a body is OMITTED rather than mapped to ``None``.
        ``variation`` treats a null body as "no value was served" and hands back the caller's
        default; a ``None`` here would make the two public read paths disagree about the identical
        wire response, and would read as "assigned nothing" when it means "not assigned".
        """
        self._assert_open()
        self._assert_flag_context(context, "all_flags")
        out: dict[str, Any] = {}
        choices, _error = self._choose(context, None, key=None, exposure=log_exposure)
        for choice in choices:
            name = choice.get("name")
            body = choice.get("body")
            if name and body is not None:
                out[name] = body
        return out

    def bool_variation(
        self,
        key: str,
        context: FlagContext,
        default_value: bool,
        *,
        disable_exposure: bool = False,
    ) -> bool:
        value = self.variation(key, context, default_value, disable_exposure=disable_exposure)
        # A served value of the wrong type is a misconfiguration, not something to coerce:
        # bool("false") is True, and a silent coercion is indistinguishable from a real answer.
        return value if isinstance(value, bool) else default_value

    def string_variation(
        self,
        key: str,
        context: FlagContext,
        default_value: str,
        *,
        disable_exposure: bool = False,
    ) -> str:
        value = self.variation(key, context, default_value, disable_exposure=disable_exposure)
        return value if isinstance(value, str) else default_value

    def number_variation(
        self,
        key: str,
        context: FlagContext,
        default_value: float,
        *,
        disable_exposure: bool = False,
    ) -> float:
        value = self.variation(key, context, default_value, disable_exposure=disable_exposure)
        # bool is a subclass of int in Python, so True would otherwise pass as the number 1.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default_value
        # Explicit: variation() is typed Any, so mypy cannot narrow through the
        # negated isinstance above and reports a bare `return value` as
        # no-any-return against the declared float.
        return float(value)

    def json_variation(
        self,
        key: str,
        context: FlagContext,
        default_value: Mapping[str, Any],
        *,
        disable_exposure: bool = False,
    ) -> Mapping[str, Any]:
        value = self.variation(key, context, default_value, disable_exposure=disable_exposure)
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
        reaches :meth:`_choose` as a transport failure and is absorbed into the caller's
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
        surfaces as a 5xx, which :meth:`_choose` absorbs into the caller's default with
        one warning line — so a client constructed without a ``source_id`` and read with only a
        ``profile_id`` would serve defaults forever and look like a working integration. Every
        other identified call in this SDK validates its identity up front; this is the same rule.
        """
        if context is None or not isinstance(context, FlagContext):
            raise IntemptConfigError(f"{method}: context must be a FlagContext")
        if not context.has_identity(self._config.source_id):
            raise IntemptConfigError(f"{method}: {IDENTITY_REQUIREMENT}")

    def _choose(
        self, context: FlagContext, names: list[str] | None, *, key: str | None, exposure: bool
    ) -> tuple[list[Mapping[str, Any]], FlagError | None]:
        """A failure returns no choices and a :class:`FlagError` rather than raising.

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
                # Absent means record, so a backend without the field behaves as before
                # (CONTRACT-R2 S4). False evaluates without spending a ONCE gate or publishing.
                "exposure": None if exposure else False,
            }
        )
        try:
            response = self._transport.post(
                self._config.project_path("/optimization/choose-api"), body
            )
        # Deliberately broad: any failure must yield the caller's default.
        except Exception as exc:
            return [], self._flag_failed(_flag_error(key, exc))
        choices = response.get("choices", []) if isinstance(response, Mapping) else None
        if not isinstance(choices, list):
            return [], self._flag_failed(FlagError(key=key, kind="parse"))
        return list(choices), None

    def _flag_failed(self, error: FlagError) -> FlagError:
        logger = self._config.logger
        if error.kind == "auth" and not self._flag_auth_logged:
            self._flag_auth_logged = True
            logger.error(
                "[intempt] flag evaluation refused with %s: check the API key (prefix %s) and "
                "source_id %s; serving defaults",
                error.status,
                self._config.credentials.prefix,
                self._config.source_id,
            )
        elif error.kind == "auth":
            logger.debug("[intempt] flag evaluation refused with %s; using defaults", error.status)
        else:
            logger.warning(
                "[intempt] flag evaluation failed (%s), using defaults",
                error.kind,
                extra={"status": error.status},
            )
        hook = self._config.on_flag_error
        if hook is not None:
            try:
                hook(error)
            except Exception:
                logger.error("[intempt] on_flag_error raised", exc_info=True)
        return error

    # -- privacy ----------------------------------------------------------

    def opt_in(self) -> None:
        self._opted_in = True

    def opt_out(self) -> None:
        """Suppress all outbound tracking writes: track, batch and commerce.

        Consent is still sent, so a withdrawal reaches the server (ruling D-5).

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

    def flush(self) -> FlushResult:
        """Drain the buffer and say how many events were sent and how many were given up on.

        Never raises for a delivery problem. Zeroes when batching is off.
        """
        if self._buffer is not None:
            return self._buffer.flush()
        return FlushResult()

    def close(self) -> FlushResult:
        """Flush within the close budget, then release the connection. Unusable after.

        This SDK's ``shutdown()``: anything still queued when the budget runs out counts as
        failed and is reported as ``dropped``.
        """
        if self._closed:
            return FlushResult()
        result = self._buffer.close() if self._buffer is not None else FlushResult()
        self._closed = True
        self._transport.close()
        return result

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
    ) -> bool:
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
        return self._submit([{"name": name, "payload": payload}])

    def _submit(self, events: list[dict[str, Any]]) -> bool:
        # A closed client raises; an opted-out client returns False. Silently
        # discarding a write after close is how events get lost without anyone
        # being told.
        self._assert_open()
        if not self.is_opted_in() or not events:
            return False
        if self._buffer is not None:
            return all([self._buffer.enqueue(event) for event in events])
        return self._send_now(events)

    def _send_now(self, events: list[dict[str, Any]]) -> bool:
        return self._deliver_now(lambda: self._send(events), len(events), event_ids(events))

    def _deliver_now(
        self, send: Any, count: int, ids: tuple[str, ...], *, label: str = "send"
    ) -> bool:
        """Direct mode: the same send table as the buffer, with no queue to keep a batch in."""
        outcome, failure = deliver(send, count=count, ids=ids, sleep=self._sleep)
        if failure is not None and outcome != "sent":
            if outcome == "kept":
                failure = DeliveryFailure(
                    kind="dropped",
                    event_count=failure.event_count,
                    event_ids=failure.event_ids,
                    terminal=True,
                )
            self._config.logger.error(
                "[intempt] %s failed; %d event(s) dropped",
                label,
                count,
                extra={"status": failure.status, "kind": failure.kind},
            )
            self._report_delivery_failure(failure)
        return outcome == "sent"

    def _report_delivery_failure(self, failure: DeliveryFailure) -> None:
        listener = self._config.on_delivery_failure
        if listener is None:
            return
        try:
            listener(failure)
        except Exception:
            self._config.logger.error("[intempt] on_delivery_failure raised", exc_info=True)

    def _send(self, events: list[dict[str, Any]]) -> bool:
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
            return False
        self._transport.post(self._track_path(), {"track": events})
        return True


def _present(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _flag_error(key: str | None, exc: Exception) -> FlagError:
    status = exc.status if isinstance(exc, IntemptApiError) else None
    if status in (401, 403):
        return FlagError(key=key, kind="auth", status=status)
    if status is not None and status >= 500:
        return FlagError(key=key, kind="server", status=status)
    if status is not None:
        return FlagError(key=key, kind="bad_request", status=status)
    timed_out = isinstance(exc, IntemptApiError) and exc.timed_out
    if timed_out or isinstance(exc, socket.timeout):
        return FlagError(key=key, kind="timeout")
    return FlagError(key=key, kind="network")


def _now_ms() -> int:
    return int(_dt.datetime.now(_dt.timezone.utc).timestamp() * 1000)
