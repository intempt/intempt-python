# Testing

## Gates, as CI actually runs them

| Gate | Command |
|---|---|
| Tests | `pytest --cov --cov-fail-under=90` |
| Mutation | `mutmut run` — **threshold 70 — deliberately lower than the other SDKs, see below** |

**The mutation gate is the real bar, not coverage.** Coverage says a line executed; mutation says an
assertion would have noticed if that line were wrong. A test asserting a value that was already true
before the code ran executes the line and kills no mutant.

## Rules

- **Tests run against a real loopback HTTP server, not a mock.** Mocking the transport proves what
  the SDK intended to send; a socket proves what went over the wire, which is the thing that breaks
  when the shape drifts from the other SDKs.
- **The assertions that matter are the failure ones.** A flag SDK is judged on what it returns when
  the service is unreachable, so an outage path without a test is an untested SDK.
- **A test that has never failed has never been tested.** Before trusting a new one, break the line
  it covers and watch it go red.
- **Assert a deliberate absence too.** A branch that is meant to do nothing — an ignored header, a
  producer that must not earn a widening — is exactly what a mutant flips without any existing test
  noticing.
- **Read the score from CI, never locally.** Local toolchains drift from CI's, and a timed-out
  mutant is counted as killed, so a loaded laptop reports a higher score than the truth.
