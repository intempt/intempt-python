"""Intempt Python SDK — server-side. Data in, decisions out.

Copyright 2026 Intempt Technologies
Licensed under the Apache License, Version 2.0.

Contains code derived from mixpanel-python (Apache License 2.0); see NOTICE.
"""

from __future__ import annotations

from ._buffer import Buffer
from ._client import COMMERCE_EVENTS, IDENTIFY_EVENT, Consent, Ecommerce, Intempt
from ._config import BatchOptions, ResolvedConfig
from ._errors import IntemptApiError, IntemptConfigError, IntemptError
from ._transport import ApiKeyCredentials, Transport

__version__ = "1.0.0"

__all__ = [
    "Intempt",
    "IntemptError",
    "IntemptApiError",
    "IntemptConfigError",
    "BatchOptions",
    "ResolvedConfig",
    "ApiKeyCredentials",
    "Transport",
    "Buffer",
    "Consent",
    "Ecommerce",
    "COMMERCE_EVENTS",
    "IDENTIFY_EVENT",
    "__version__",
]
