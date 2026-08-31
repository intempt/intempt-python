"""Feature flags, experiments and personalizations, read by key.

The cross-SDK surface is defined in ``intempt-swift/docs/SDK-API-CONTRACT.md``, which every
Intempt SDK conforms to. Four of its rules shape this module:

1. The caller asks for a KEY, never a mode. The older surface put the mode in the method name,
   which forced an integrator to know whether a key was an experiment before reading it and grew
   combinatorially with every new mode. The platform resolves mode itself: its serving query
   filters on channel and status and never on mode.
2. ``default_value`` is REQUIRED. It is what a caller receives on a network failure, a timeout,
   an unknown key or a malformed response. Optional is how ``None`` reaches production during an
   outage.
3. ``variation_detail`` is NOT exposed. It would carry a ``reason``, and the platform does not
   send one, so it could not tell a deliberate off
   state from a request the service never answered.
4. Evaluation is REMOTE only. There is no local rule engine and no flag store to poll.

A server SDK is an ``api``-channel consumer: it receives a value and branches on it in code.

Two service-side rules are enforced here rather than discovered as a silent default:

* The evaluation endpoint answers a PROFILE identity (``profile_id`` plus a configured
  ``source_id``) or a USER identity (``user_id``), and raises on anything else. A context that
  satisfies neither is rejected at the call site — see :data:`IDENTITY_REQUIREMENT`.
* A key must match :data:`KEY_PATTERN`. The service applies the same expression and answers a
  request that violates it with a 400, which a flag SDK would otherwise absorb into the caller's
  default forever.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

FlagReason = Literal["targeted", "holdout", "not_targeted", "off"]

#: A response the service did not answer is reported as such rather than guessed at.
UNANSWERED: FlagReason = "off"

#: The charset the service validates ``names`` against, copied from
#: ``ExperienceApiChooseRequest.java``. A key outside it is answered with a 400, and a 400 on a
#: flag read is indistinguishable from an unknown key once it has been absorbed into the default.
KEY_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

#: What the evaluation endpoint needs before it can answer at all. ``buildAudienceRequest`` takes
#: the PROFILE branch when a source id and a non-blank profile id are both present, falls to USER
#: when a user id is present, and otherwise raises — which reaches a flag caller as a permanent,
#: silent default.
IDENTITY_REQUIREMENT = (
    "context needs either user_id, or profile_id together with a source_id configured on the client"
)


@dataclass(frozen=True)
class FlagContext:
    """Who is being evaluated.

    ``profile_id`` is the anonymous/device identifier and is paired with the client's
    ``source_id``. Assignment derives from ONE identifier, not from the pair: the service builds
    its key from ``user_id`` when one is present and from ``source_id``/``profile_id`` only when
    it is not. So passing both does NOT bridge a sign-in — a person read anonymously and then
    read again with a ``user_id`` is keyed differently, and holding one identifier constant is
    what keeps an assignment stable.

    ``session_id`` is what scopes an exposure. An experience whose display is ``once`` or
    ``once_per_visit`` is served against a stored session value, and a request without one is
    stored under a single shared placeholder — so the SECOND read of such a key returns nothing
    and the caller receives the default. Supply a session identifier whenever the key might be
    display-limited, and expect every exposure to collapse into one bucket when you do not.

    There is no ``account_id``: the evaluation endpoint's ``Identification`` carries
    ``sourceId``/``profileId``/``userId`` and nothing else, so a value set here would be silently
    dropped rather than used.
    """

    user_id: str | None = None
    profile_id: str | None = None
    session_id: str | None = None

    def has_identity(self, source_id: str | None) -> bool:
        """Whether the service can answer this context at all.

        Mirrors ``ExperienceChooserService.buildAudienceRequest``: the PROFILE branch needs a
        source id and a non-blank profile id, the USER branch needs a user id, and there is no
        third branch — it raises.
        """
        if _present(self.user_id):
            return True
        return _present(self.profile_id) and _present(source_id)


def _present(value: str | None) -> bool:
    return isinstance(value, str) and bool(value.strip())


@dataclass(frozen=True)
class FlagDetail:
    """A value and why it was returned."""

    value: Any
    reason: FlagReason
