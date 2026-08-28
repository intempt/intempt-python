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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

FlagReason = Literal["targeted", "holdout", "not_targeted", "off"]

#: A response the service did not answer is reported as such rather than guessed at.
UNANSWERED: FlagReason = "off"


@dataclass(frozen=True)
class FlagContext:
    """Who is being evaluated.

    ``profile_id`` is the anonymous/device identifier. Supplying the same value before and after a
    person signs in is what keeps their assignment stable across the transition.
    """

    user_id: str | None = None
    account_id: str | None = None
    profile_id: str | None = None


@dataclass(frozen=True)
class FlagDetail:
    """A value and why it was returned."""

    value: Any
    reason: FlagReason
