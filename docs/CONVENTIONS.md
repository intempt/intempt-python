# Conventions

**The cross-SDK surface is not decided here.** Every Intempt SDK conforms to
`intempt-swift/docs/SDK-API-CONTRACT.md`, which is the single authority on method names, argument
order, defaults and what is deliberately withheld. This file covers what is specific to Python and
to this repo. Where the two disagree, the contract wins and this file is the bug.

## The rules that come from the contract

- **A caller asks for a KEY, never a mode.** There is no `flagVariation` / `experimentVariation`
  split. The platform resolves whether a key is an experiment, a personalization or a flag; its
  serving query filters on channel and status and never on mode. A method name that encodes the
  mode forces an integrator to know the answer before they can ask the question, and grows
  combinatorially with every mode added.
- **`defaultValue` is REQUIRED, everywhere, and is never optional.** It is what a caller receives on
  a network failure, a timeout, an unknown key or a malformed response. A flag SDK that throws when
  the service is unreachable takes the application down with it, which is the opposite of what a
  kill switch is for.
- **A wrong-typed value falls back; it is never coerced.** A flag configured as a string and read as
  a boolean returns the caller's default, not `true`. Coercion makes a misconfiguration look like a
  deliberate value.
- **`variationDetail` is NOT exposed.** It would carry a reason, and the serving response does not
  send one — so it could only report "off" for a person who was in fact targeted and served, which
  is the single thing such a method exists to tell you. It stays internal until the platform sends
  a reason. Do not re-add it, and do not document it.
- **Evaluation is REMOTE only.** No local rule engine, no flag store to poll, and no hashing
  utility: the server buckets, so no second implementation can disagree with it. `check-no-local-bucketing.mjs`
  enforces this in CI and a new bucketing helper will fail the build.
- **A validation mistake throws; a service problem does not.** A blank key or a missing default is a
  programming error the caller can fix, so it fails loudly at the call site. A 5xx is a runtime
  condition to absorb.

## What the flag path validates before it sends

The three checks below exist because the alternative is not an error — it is the caller's default,
returned forever, with one warning line. A flag that is dead in production is indistinguishable
from a flag deliberately serving its default, so each of these fails at the call site instead.

- **The identity must be one the service can answer.** `buildAudienceRequest` takes the PROFILE
  branch on a source id plus a non-blank profile id, falls to USER on a user id, and otherwise
  raises. `FlagContext` is checked against exactly that before a request is made.
- **The key must match `^[a-zA-Z0-9_-]+$`.** The service validates every name against that
  expression and answers a violation with a 400 — which the flag path absorbs identically to a key
  that was never created. `pricing.cta` is a typo you want to find in development.
- **`device` is sent on every request and is load-bearing.** A null device becomes the SQL
  predicate `"0"` server-side, which is false for every row, so omitting it returns zero
  experiences rather than an error. `tests/test_flags.py` asserts it goes over the wire.

`session_id` is not validated because it is genuinely optional, but it is not inert: an experience
whose display is `once` or `once_per_visit` is served against a stored session value, so without
one the second read of such a key returns nothing and every exposure lands in one bucket.

## Errors

Two tiers, and they are not interchangeable: a configuration mistake surfaces when the config is
built, and an API failure carries the status, the body and any `Retry-After`. A transport failure
that never produced a response carries a **null** status — read as retryable, because a request
that never arrived may well arrive next time, whereas a 400 fails identically however often it is
repeated.

## Wire shape

The ingest envelope is shared byte-for-byte across the server SDKs:

```
{"track": [{"name": "<event>", "payload": [{eventId, timestamp, profileId?, userId?, accountId?,
                                            data?, userAttributes?, accountAttributes?}]}]}
```

**An absent field is omitted, never sent as null** — a present key is an assertion about the entity.
A divergence here does not fail any test; it ingests cleanly and never appears in a report.

## Credentials

**What this SDK does, which is the only part settled here.** One `api_key` in `<prefix>.<secret>`
form serves ingest and evaluation alike, sent as HTTP **Basic** — not Bearer — on every request
including `POST /optimization/choose-api`. Never log the credential and never put it in a URL.

**What is NOT settled: whether evaluation should require a credential distinct from the ingest
one.** An earlier draft of this file asserted that the endpoint takes a *server* credential and
refuses a public key. That assertion is withdrawn: it was never verified against the service, and
it contradicted this SDK's own transport, which accepts a key its error text calls public and then
sends it to that endpoint. If it had been true, the flag surface could not work at all.

The question behind it is real — the evaluation response describes how every experience in the
project targets, which is not obviously public — but it is a **product decision about the
credential model, and it is unruled**. It is not settleable inside an SDK repo, and this file must
not settle it by describing a rule nobody made. **Owner: Sid.** Until it is ruled, treat the
paragraph above as the contract and do not re-add a claim about what the endpoint refuses without
a reading of the service's security configuration to cite.

## Python specifics

- **Naming follows the language, not the contract's spelling.** The contract's `allFlags` is
  `all_flags` here and `variationDetail` is `_variation_detail`; the shape is identical, the case is
  not. A leading underscore is how this SDK expresses "withheld".
- **The mutation threshold is 70, not 85.** That is a measured decision, not an oversight: mutmut's
  operator set produces a materially different denominator from Stryker's and Infection's, and 70 here
  was the level at which surviving mutants were genuinely equivalent rather than untested. Raising it
  to match the others without re-measuring would be cargo-culting a number across tools.
