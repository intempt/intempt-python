# Intempt Python SDK

Server-side client for [Intempt](https://intempt.com). **Data in, decisions out.**

- **In** — events, identity, consent, commerce
- **Out** — recommendations from your feeds

This is a server library, not a browser one. It holds no per-user state: every call
takes its identifier explicitly, so one client instance is safe to share across
threads and requests for all users.

```bash
pip install intempt
```

Requires Python 3.9 or newer. No runtime dependencies.

## Quick start

```python
from intempt import Intempt

intempt = Intempt(
    org="my-org",
    project="my-project",
    api_key=os.environ["INTEMPT_API_KEY"],  # "<prefix>.<secret>"
    source_id="684508596718616576",
)

intempt.track("purchase", user_id="user@example.com", properties={"total": 99.99})

feed = intempt.recommend(
    user_id="user@example.com",
    feed_id="5292",
    fields=["id", "title"],
)
```

By default each call sends one request and returns when the server responds. Nothing
is buffered, so there is nothing to lose on exit — which makes this safe in Lambda and
other short-lived processes.

## API

| call | returns | endpoint |
| ---- | ------- | -------- |
| `Intempt(**config)` | client | — |
| `track(event, **options)` | `None` | `POST …/track` |
| `track_batch(events)` | `None` | `POST …/track`, chunked |
| `identify(**options)` | `None` | `POST …/track` (reserved `Identify`) |
| `group(account_id=…, **options)` | `None` | `POST …/track` (reserved `Identify`) |
| `alias(user_id=…, previous_user_id=…)` | `None` | `POST …/track` (reserved `Identify`) |
| `consent.grant(**options)` | `None` | `POST …/consents/data` |
| `consent.revoke(**options)` | `None` | `POST …/consents/data` |
| `ecommerce.product_viewed(product_id=…, **ids)` | `None` | `POST …/track` |
| `ecommerce.added_to_cart(product_id=…, quantity=…, **ids)` | `None` | `POST …/track` |
| `ecommerce.ordered(products=[…], **ids)` | `None` | `POST …/track` |
| `recommend(feed_id=…, fields=[…], **ids)` | `dict` | `POST …/feeds/{id}/data` |
| `opt_in()` / `opt_out()` / `is_opted_in()` | — | — |
| `flush()` / `close()` | `None` | — |
| `set_config(**patch)` / `config` / `buffered` | — | — |

Every method raises on failure. Nothing is swallowed.

## Identifiers

Every call takes at least one of two, and both are values you already own:

| | |
| --- | --- |
| `user_id` | your identifier for a person: an email, an internal user id |
| `account_id` | your identifier for a company or account |

That is the whole list. The platform resolves identity from `user_id` itself.

Two platform identifiers are deliberately **not** exposed:

- **`profile_id`** is the anonymous id the browser SDK mints and keeps on the device.
  A server that invents one creates an orphan profile that never stitches to a real
  visitor.
- **`master_id`** is assigned internally after identity resolution. There is no way to
  look one up from here, and a hardcoded one breaks the moment two profiles merge.

## Batching

Off by default. Turn it on for a long-lived process:

```python
from intempt import Intempt, BatchOptions

intempt = Intempt(
    org="my-org",
    project="my-project",
    api_key=key,
    batch=BatchOptions(size=50, flush_ms=5_000, max_queue=10_000),
)

intempt.track("page_view", user_id="u1")  # buffered
intempt.flush()  # send now
intempt.close()  # drain and release
```

`flush()` and `close()` are safe to call when batching is off; they do nothing.

`close()` drains for at most 30 seconds, then stops retrying and logs how many events
it gave up on. Without a ceiling a shutdown hook can block for minutes against a
failing endpoint. `flush()` is **not** bounded: a caller who is not shutting down has
not asked to give up.

### Retry policy

| response | behaviour |
| -------- | --------- |
| 413, batch > 1 | halve the batch size, retry |
| 413, batch = 1 | drop the event, log it, return the width to full |
| 429 | honour `Retry-After`, else exponential backoff |
| 5xx, 408, timeout | exponential backoff, floored at 100ms, capped at 10 min |
| other 4xx | drop the batch, log the status and body |
| 5 consecutive failures | stop batching and say how many events are stranded |
| 3 consecutive 413 drops | say the gateway body limit is the likely cause, once |

A 413 halves the width, and the width only widens again after ten consecutive
successful sends that filled it, doubling each time. One transient 413 costs
throughput for a while rather than forever.

The buffer is in memory. A hard crash loses it.

**Delivery is at-least-once, not exactly-once.** A retry after a lost response
re-sends events the server may already have stored, and ingestion has no idempotency
key — `eventId` travels in the payload but is not a column in the events table, and
the table is a plain `MergeTree`, so nothing collapses duplicates. Set `batch=None` if
you would rather a failure surface to your code than be retried, and de-duplicate
downstream if exact counts matter.

## Timestamps

`timestamp` accepts a `datetime` or epoch milliseconds. A naive `datetime` is treated
as UTC, not local time.

It **is** a backfill mechanism, between 2010 and 2040. The event store keeps your value
and records arrival separately, so the two never overwrite each other.

| your timestamp | what happens |
| -------------- | ------------ |
| before 2010-01-01 | request rejected with an error naming the threshold |
| 2010 to 2040 | stored as given |
| after 2040-01-18 | **silently replaced with the server's current time** |

A timestamp in seconds where milliseconds were meant lands in the far future, sails
past the upper bound, and is quietly rewritten to now — no error, and the event looks
like it just happened. The reverse mistake fails loudly.

## Opting out

```python
intempt.opt_out()  # suppresses track, batch, commerce and consent
intempt.opt_in()
```

The gate is applied when events are sent, not only when they are captured, so events
buffered before `opt_out()` are discarded rather than transmitted by a later flush.

## Not in this SDK

Console and configuration operations — journeys, dashboards, segments, brand — belong
to the CLI and the MCP server, not to a data-plane SDK.

Experiments and personalizations are also absent: they resolve a web experience
against a page, and a server has no page to modify. Use the browser SDK for those.

## License

Apache 2.0. Contains code derived from
[mixpanel-python](https://github.com/mixpanel/mixpanel-python), also Apache 2.0; see
[NOTICE](./NOTICE) for what was taken and what was changed.
