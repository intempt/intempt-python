# Samples

Two of them. Both are executed by the test suite on every CI run, so neither can
drift from the SDK without turning something red.

| | what it is | start here if |
| --- | --- | --- |
| [`bare/send.py`](bare/send.py) | one client, one of each call, then close | you are evaluating the SDK or checking a credential |
| [`basic/app.py`](basic/app.py) | an HTTP server that instruments itself | you are wiring the SDK into a real service |

## Credentials

Both read the same environment. `INTEMPT_ORG`, `INTEMPT_PROJECT` and
`INTEMPT_API_KEY` are required; the rest are optional.

```bash
export INTEMPT_ORG=my-org
export INTEMPT_PROJECT=my-project
export INTEMPT_API_KEY='prefix.secret'
export INTEMPT_SOURCE_ID=684508596718616576   # optional, but recommended
export INTEMPT_FEED_ID=5292                   # optional, enables recommend
```

`INTEMPT_SOURCE_ID` is a 19-digit number. Keep it a string — a numeric round trip
loses the last digits and addresses a different source with no error.

## bare/send.py

```bash
python examples/bare/send.py
```

Sends eight requests and exits: identify, track, group, three commerce calls, a
consent record, and the feed read when `INTEMPT_FEED_ID` is set. Every call sends
one request and returns when the server answers, so the events are in the console
by the time it exits.

Exit codes: `0` sent, `1` the API refused something, `2` bad arguments or missing
environment.

## basic/app.py

```bash
python examples/basic/app.py

# then, in another shell
curl -X POST localhost:8080/signup   -d 'user=ada@example.com'
curl -X POST localhost:8080/purchase -d 'user=ada@example.com&sku=21&qty=2'
curl        'localhost:8080/recommend?user=ada@example.com'
curl -X POST localhost:8080/forget   -d 'user=ada@example.com'
```

The point is the shape, not the routes: one client for the whole process, an
identifier on every call, batching on, and a shutdown that drains.

## Pointing them somewhere else

Both honour `INTEMPT_HOST` and `INTEMPT_SCHEME`, which is how the test suite runs
them against a loopback server. A sample nobody can point elsewhere is a sample
nobody can test, including whoever wrote it.

```bash
INTEMPT_HOST=127.0.0.1:8931 INTEMPT_SCHEME=http python examples/bare/send.py
```
