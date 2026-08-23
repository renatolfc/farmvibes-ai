# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from unittest.mock import patch

from vibe_core.cli.osartifacts import OSArtifacts
from vibe_core.cli.wrappers import AzureCliWrapper


def test_get_storage_account_key_accepts_wrapped_azure_cli_response() -> None:
    az = AzureCliWrapper(OSArtifacts(), "cluster", "resource-group")

    with patch(
        "vibe_core.cli.wrappers.execute_cmd",
        return_value='{"keys": [{"value": "storage-key"}]}',
    ):
        assert az.get_storage_account_key("storage") == "storage-key"
