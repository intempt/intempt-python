# Security policy

## Reporting a vulnerability

Email **security@intempt.com**. Don't open a public issue - a public report is a disclosure, and it
puts every customer running this SDK at risk before there's a fix.

Tell us what you found, how to reproduce it, and what an attacker could do with it. If you have a
proof of concept, include it.

We'll acknowledge within two business days and tell you what we're doing about it.

## What's in scope

Anything in this repository, including:

- A credential, key or token committed to the repo or its history
- A way to read or send another customer's data through the SDK
- A payload that crashes or hangs the host app
- Anything that sends data the integrator didn't ask to send

## What's out of scope

- Vulnerabilities in a dependency we don't control, unless this SDK's use of it is what makes it
  exploitable. Report those upstream, then tell us.
- Findings from a scanner with no working proof of concept.
- Anything requiring physical access to an unlocked device, or a rooted or jailbroken OS.

## Credentials

Intempt API keys are `<id>.<secret>` and are sent as HTTP Basic. **If you find one in this repo, in
its history, or in a published artifact, that's a valid report** - tell us and we'll rotate it.

Client SDK keys are ingestion-scoped and write-only by design. They can't read data back. That still
makes a leaked one worth rotating, because it can be used to write junk into your workspace.

## Supported versions

We fix security issues on the latest published major version. Older majors get a fix only if the
issue is exploitable without an integrator changing their code.
