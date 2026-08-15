# Changelog

## 1.0.0 — unreleased

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
