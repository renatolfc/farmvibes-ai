# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from copy import deepcopy

from vibe_core.security import (
    API_AUTH_SECRET_KEY,
    API_AUTH_SECRET_NAME,
    API_TOKEN_ENV_VAR,
    BEARER_SCHEME,
    REDACTED_VALUE,
    REMOTE_API_TOKEN_FILENAME,
    redact_sensitive,
)


def test_remote_api_security_names_are_stable():
    assert API_TOKEN_ENV_VAR == "FARMVIBES_API_TOKEN"
    assert REMOTE_API_TOKEN_FILENAME == "remote_api_token"
    assert API_AUTH_SECRET_NAME == "farmvibes-api-auth"
    assert API_AUTH_SECRET_KEY == "token"
    assert BEARER_SCHEME == "Bearer"


def test_redact_sensitive_recurses_case_insensitively_without_mutating_input():
    original = {
        "plain": "visible",
        "Secret": "secret",
        "pc_key": "planetary-computer-key",
        "app_key": "ambient-weather-key",
        "nested": [
            {
                "PASSWORD": "password",
                "auth-token": "token",
                "clientCredential": "credential",
            },
            (
                {
                    "api_key": "api key",
                    "Subscription Key": "subscription key",
                    "accessKey": "access key",
                    "private-key": "private key",
                    "ConnectionString": "connection string",
                },
            ),
        ],
    }
    untouched = deepcopy(original)

    redacted = redact_sensitive(original)

    assert original == untouched
    assert redacted is not original
    assert redacted["plain"] == "visible"
    assert redacted["Secret"] == REDACTED_VALUE
    assert redacted["pc_key"] == REDACTED_VALUE
    assert redacted["app_key"] == REDACTED_VALUE
    assert set(redacted["nested"][0].values()) == {REDACTED_VALUE}
    assert set(redacted["nested"][1][0].values()) == {REDACTED_VALUE}
