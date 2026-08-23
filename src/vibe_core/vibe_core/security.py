# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from copy import deepcopy
from typing import Any, Final

API_TOKEN_ENV_VAR: Final[str] = "FARMVIBES_API_TOKEN"
REMOTE_API_TOKEN_FILENAME: Final[str] = "remote_api_token"
API_AUTH_SECRET_NAME: Final[str] = "farmvibes-api-auth"
API_AUTH_SECRET_KEY: Final[str] = "token"
BEARER_SCHEME: Final[str] = "Bearer"
REDACTED_VALUE: Final[str] = "***REDACTED***"

_SENSITIVE_KEYS = (
    "secret",
    "password",
    "token",
    "credential",
    "apikey",
    "subscriptionkey",
    "accesskey",
    "privatekey",
    "connectionstring",
)


def _is_sensitive_key(key: str) -> bool:
    normalized = "".join(c for c in key.casefold() if c.isalnum())
    lowered = key.casefold()
    return any(sensitive in normalized for sensitive in _SENSITIVE_KEYS) or (
        lowered == "key" or lowered.endswith(("_key", "-key", " key"))
    )


def redact_sensitive(value: Any) -> Any:
    """Return a copy with values under sensitive keys redacted recursively."""

    if isinstance(value, dict):
        return {
            deepcopy(key): (
                REDACTED_VALUE
                if isinstance(key, str) and _is_sensitive_key(key)
                else redact_sensitive(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in value)
    return deepcopy(value)
