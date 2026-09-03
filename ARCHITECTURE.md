# Server SDK architecture

One contract, three implementations: `intempt-node`, `intempt-python`, `intempt-php`.

This file is the same in all three repos. Change it in one, change it in all, or the
SDKs drift and a customer switching languages gets different delivery semantics for
the same call.

## Where this came from

The shape is derived from [mixpanel-node] / [mixpanel-python] / [mixpanel-php]: the
consumer/buffer split, the chunking, the keep-alive connection reuse and the bounded
concurrency are theirs. Both projects are Apache 2.0 and this one retains their
notices; see `NOTICE`.

What is **not** inherited is the delivery guarantees. mixpanel-node has no request
timeout, no retry, and discards errors when no callback is passed
(`lib/mixpanel-node.js:80`, `callback = callback || function () {}`). mixpanel-python
is stronger — it has real backoff — but its retry policy is still not ours. Every
guarantee below was added deliberately.

[mixpanel-node]: https://github.com/mixpanel/mixpanel-node
[mixpanel-python]: https://github.com/mixpanel/mixpanel-python
[mixpanel-php]: https://github.com/mixpanel/mixpanel-php

## The surface

Identical across all three, allowing for language idiom (`snake_case` in Python,
`camelCase` in Node/PHP).

| call | endpoint |
| ---- | -------- |
| `track(event, options)` | `POST …/sources/{sourceId}/track` |
| `track_batch(events)` | same, chunked at `max_request_events` |
| `identify(options)` | same, reserved event `Identify` |
| `group(options)` | same, reserved event `Identify` |
| `consent.grant(options)` / `consent.revoke(options)` | `POST …/consents/data` |
| `ecommerce.product_viewed / added_to_cart / ordered` | `POST …/track` |
| `recommend(options)` | `POST …/feeds/{feedId}/data` |
| `opt_in()` / `opt_out()` / `is_opted_in()` | — |
| `flush()` / `close()` | — |
| `set_config(patch)` / `config` / `buffered` | — |

Base path is `{path}/v1/{org}/projects/{project}`. Without a `source_id` the track
path is `…/track` rather than `…/sources/{id}/track`.

### Deliberately absent

- **Experiments and personalizations.** They resolve a web experience against a page.
  A server has no page. These belong to the browser SDK.
- **`profile_id` and `master_id`.** `profile_id` is the anonymous id the browser SDK
  mints on the device; a server that invents one creates an orphan profile that never
  stitches to a real visitor. `master_id` is assigned internally after identity
  resolution, cannot be read from here, and breaks the moment two profiles merge.
  The only identifiers are `user_id` and `account_id`, both values the caller owns.
- **Console and configuration operations.** Journeys, dashboards, segments and brand
  belong to the CLI and MCP server, not to a data-plane SDK.

## Delivery guarantees

### Errors surface. Nothing is swallowed

Every method raises/rejects on failure. No callback-swallow, no silent discard. This
is the single biggest departure from mixpanel-node.

### Retry policy

| response | behaviour |
| -------- | --------- |
| 413, batch > 1 | halve the batch width, retry |
| 413, batch = 1 | drop the event, log it, return the width to full |
| 429 | honour `Retry-After`, else exponential backoff |
| 5xx, 408, timeout | exponential backoff, floored at 100ms, capped at 10 min |
| other 4xx | drop the batch, log status and body |
| 5 consecutive failures | stop batching, report how many events are stranded |
| 3 consecutive 413 drops | say the gateway body limit is the likely cause, once |

Two width rules that are easy to get wrong, and were:

1. **A reduced width must recover.** Compare successes against the *current* width,
   not the full width — `batch` is sliced to the current width, so a comparison
   against full is unreachable and the reduction becomes permanent. Widen by doubling
   after 10 consecutive successful sends that filled the width.
2. **The drop tally must not change behaviour.** It is diagnostic only. Stopping on it
   strands the queue; pinning the width to 1 on it caps throughput to one event per
   round trip and loses more events than it saves requests.

### Timestamps

Client timestamps are honoured by the platform between 2010 and 2040. Below
`LOW_TIMESTAMP_LIMIT` the request is rejected; **above `UP_TIMESTAMP_LIMIT` the server
silently replaces the value with its own clock**. Send milliseconds. `/track` is
milliseconds, `/consents/data` is **seconds** — the consent path divides.

### Platform ids are strings, never numbers

A 19-digit snowflake exceeds every language's safe integer range.
`Number("1841710181319290880")` rounds to `1841710181319290900` and addresses a
different source. Never coerce an id numerically, in any language, at any layer.

### Delivery is at-least-once

Ingestion has no idempotency key. `event_id` travels in the payload but is not a
column in the events table, and the table is a plain `MergeTree` — nothing collapses
duplicates. A retry after a lost response therefore duplicates rows. Say so in the
README rather than letting callers infer exactly-once from the retry table.

### close() is bounded

`close()` drains for at most 30 seconds, then stops retrying and logs how many events
it abandoned. Unbounded, a shutdown hook blocks for minutes against a failing
endpoint. `flush()` is **not** bounded — a caller who is not shutting down has not
asked to give up.

Enforce the deadline in two places, because one leaks: before starting a backoff (the
first wait alone can exceed the whole budget) and at the top of each drain iteration
(sends that succeed but are slow never reach the backoff check).

### opt-out gates buffered events too

The gate lives in the send path, not only at the call site. Otherwise events captured
before `opt_out()` are still transmitted by a later `flush()`, `close()` or exit hook,
and a consent revocation between capture and flush is not honoured.

## Quality bar

Every implementation ships with:

| gate | threshold |
| ---- | --------- |
| unit tests, HTTP intercepted | full method surface |
| integration tests, real socket | framing, keep-alive, timeout, concurrency |
| statement coverage | 100% |
| mutation score | **≥ 85%, enforced in CI as its own job** |
| lint + format + type check | clean, tests included in the type check |
| contract test against production | every method, negative controls included |

Mutation testing is not optional decoration. On the Node SDK it found two defects that
100% statement coverage did not: a 413 that permanently halved throughput because the
recovery condition was unreachable, and four dead conditionals that could not fail.
It also found three assertions that were green and asserted nothing.

Read the mutation score from CI, never from a developer machine. A timed-out mutant is
scored as killed, so a loaded laptop inflates the number — 86.19% locally against
84.70% on CI for the same commit, with a gate at 85.

## Contract test inputs

Every id must be real. Ingestion returns **201 for unknown accounts and products**, so
a fabricated `product_id` is a green test that proves nothing. Any missing input makes
its step SKIP, never silently pass.

Read endpoints need a negative control. A missing feed and a real feed with no matches
both answer 200 with an empty array, so an empty result proves nothing on its own. A
bogus feed id answers 400, and that is what makes the 200 meaningful.
