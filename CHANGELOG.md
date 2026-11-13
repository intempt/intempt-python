# Changelog

## 3.0.0 — unreleased

### Changed — breaking

- **Each request is sent once** (INT-7436). 503 and 429 get one resend after
  `Retry-After` clamped to 1–60 s (1 s when absent); a wait over 5 s drops the batch
  instead of sleeping. Every other answered status, 413 included, and a timeout after
  the request was written are dropped and reported. A batch that never reached the
  server stays queued for the next flush. The exponential backoff, the circuit breaker
  and the 413 halving are gone. `IntemptApiError.retryable` is now true for 429 and 503
  only; new `reached` and `timed_out` attributes say how a transport failure happened.
  A pooled keep-alive socket the server already closed is not a lost batch: the
  transport reconnects once at once and sends on the fresh socket.
- **A kept batch waits for the timer, and the queue cap evicts the oldest event**
  (ruling R4). While a batch that never reached the server is kept, reaching `size`
  no longer sends; the batch goes out at the next `flush_ms` tick or an explicit
  `flush()`. At `max_queue` the oldest queued event is dropped and reported `dropped`
  and the new event is queued (`track` returns True); before, the new event was refused.
- **Writes no longer raise for a delivery problem** (INT-7444). `track`, `track_batch`,
  `identify`, `group`, `consent.grant/revoke` and the `ecommerce` methods return `bool`
  (queued in batch mode, delivered in direct mode). A new `on_delivery_failure` option
  receives a `DeliveryFailure` once per batch on its final outcome. `flush()` and
  `close()` return `FlushResult(sent, failed)`. Code that caught `IntemptApiError`
  around a write must read the return value or the listener instead. Bad arguments and
  a closed client still raise; `recommend()` still raises.
- **`all_flags()` no longer records exposures** (INT-7456). It sends `exposure: false`,
  so a bulk read does not count every key as seen; read the key you show with
  `variation()`, or pass `all_flags(..., log_exposure=True)`. Needs the matching
  audience-service change; until then the service ignores the field.

### Added

- **`on_flag_error` and public `variation_detail()`** (INT-7454). A failed evaluation
  still returns the default, and now calls `on_flag_error(FlagError(key, kind, status))`
  with `kind` one of `auth`, `bad_request`, `server`, `timeout`, `network`, `parse`.
  `variation_detail()` returns `FlagDetail(value, reason, error)` with `reason="error"`
  on a failure; a 200 with no choice stays `off`. 401/403 is logged at error level once
  per client, naming the key prefix and `source_id`, then at debug.
- **`disable_exposure=True`** on `variation`, `variation_detail` and the typed variants
  (INT-7456), for a read that does not display the value.

## 2.1.0 — unreleased

### Changed

- **`source_id` is validated at init** (INT-7455). It must be a positive 64-bit
  integer as a string of digits (`^[1-9][0-9]{0,18}$`, at most
  9223372036854775807), which is what the API parses. A value such as `"abc"`,
  `"0"` or a 20-digit id used to pass init and then fail every track and consent
  call; it now raises.
- **Consent is sent while opted out** (INT-7437, ruling D-5). After `opt_out()`
  consent records were dropped silently, so a person who had opted out could not
  withdraw consent. Grant and revoke are now sent regardless of opt-out, as the
  client SDKs do; tracking writes stay suppressed.
- **A flag's detail reason no longer reads `reason` off the choose response**
  (INT-7457). choose-api never sends one; the reason is the fixed `UNANSWERED`,
  as intempt-php already reports. No public effect.

### Fixed

- **Buffer drop diagnostics no longer raise `KeyError` from a stdlib logger**
  (INT-7440). They passed `extra={"name": ...}`, a reserved `LogRecord` key, so
  with ERROR enabled (the default) `track()` on a stopped buffer or full queue,
  and `flush()` on a single-event 413, raised. The key is now `event_name`.

## 2.0.0 — 2026-09-03

### Removed

- **`alias()` — breaking.** Linking two user identities is the platform's job,
  not the caller's. Ingestion already converges two user ids that ever share an
  identifier — device, email, phone — onto one root, with nobody declaring
  anything. `alias()` only reached the case of two ids that never co-occur,
  which is an id-scheme migration and belongs in a server-side backfill. A
  wrong call permanently fused two real people and there is no unmerge.
  `identify()` is unchanged and remains the stitch trigger.

## 1.1.0 — 2026-08-31

Flags, experiments and personalizations become readable from the server. Additive:
nothing in 1.0.0 changed behaviour, and no existing method, argument or export was
touched, so upgrading from 1.0.0 cannot change what an existing integration does.

### Added

- `variation`, `bool_variation`, `string_variation`, `number_variation` and
  `json_variation` read one key; `all_flags` reads every key assigned to a person
  in one call. `wait_for_initialization` is present and inert, because evaluation
  here is remote and there is no local store to wait for.
- `FlagContext(user_id=…, profile_id=…, session_id=…)` says who is being
  evaluated. `session_id` is what makes a key whose display is `once` or
  `once_per_visit` readable more than once; without one the platform stores a
  single shared placeholder session and the second read returns your default.
- `default_value` is required on every read, and is what you receive on a network
  failure, a timeout, an unknown key or a malformed response. A service problem
  never raises out of a flag read; a caller mistake — an unusable context, or a
  key outside `[a-zA-Z0-9_-]` that the platform answers with a 400 — raises
  immediately rather than resolving to your default forever.

### Not exposed

- **`variation_detail`.** It would carry a `reason`, and the platform does not
  send one, so it could not tell a deliberate off state from a request that was
  never answered. `FlagDetail` and `FlagReason` stay internal for the same reason.
- **Local evaluation.** No rule engine, no flag store to poll, and no bucket
  arithmetic — which side derives a bucket is a correctness question rather than a
  performance one, and CI fails the build on a hashing primitive in `src`.

### Corrects 1.0.0

- 1.0.0 listed **experiments and personalizations** under *Deliberately absent*, on
  the reasoning that they resolve a web experience against a page. That holds for
  the `web` channel and not for the `api` one: a server SDK receives a value and
  branches on it in code. The 1.0.0 entry below is left as written — it was true of
  1.0.0 — and this is the correction.

## 1.0.0 — 2026-08-16

First release. Server-side SDK, Apache 2.0, derived from mixpanel-python; see
[NOTICE](./NOTICE) for what was taken and what changed.

### The surface

`track`, `track_batch`, `identify`, `group`, `alias`, `consent.grant/revoke`,
`ecommerce.product_viewed/added_to_cart/ordered`, `recommend`,
`opt_in`/`opt_out`/`is_opted_in`, `flush`, `close`, `set_config`, `config`,
`buffered`.

Identical to `intempt-node` and `intempt-php` allowing for language idiom, so a
customer switching languages gets the same delivery semantics for the same call.
The shared contract is in [ARCHITECTURE.md](./ARCHITECTURE.md).

### Deliberately absent

- **Experiments and personalizations.** They resolve a web experience against a
  page, and a server has no page. Browser SDK territory.
- **`profile_id` and `master_id`.** The only identifiers are `user_id` and
  `account_id`, both values the caller already owns.
- **Console and configuration operations.** Journeys, dashboards, segments and
  brand belong to the CLI and MCP server.

### Delivery guarantees

- Every method raises on failure. Nothing is swallowed.
- Retry policy: 413 halves the batch width and recovers by doubling after ten
  full-width successes; 429 honours `Retry-After`; 5xx/408/timeout back off
  exponentially, floored at 100ms and capped at 10 minutes; other 4xx drop the
  batch; five consecutive failures stop batching and report what is stranded.
- `close()` is bounded at 30 seconds and says how many events it abandoned.
  `flush()` is unbounded.
- Opt-out is enforced in the send path, so events buffered before `opt_out()`
  are discarded rather than transmitted by a later flush.
- A platform id never goes through `int()`. A 19-digit snowflake exceeds float
  precision and a numeric round trip addresses a different source.
- Consent timestamps are epoch **seconds**; `/track` is milliseconds.
- Delivery is at-least-once. Ingestion has no idempotency key, so a retry after
  a lost response duplicates rows.
