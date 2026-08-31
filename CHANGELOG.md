# Changelog

## 1.1.0 — unreleased

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
