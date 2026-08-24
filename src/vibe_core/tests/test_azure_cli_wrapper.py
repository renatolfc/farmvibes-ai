# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from vibe_core.cli import remote
from vibe_core.cli.osartifacts import KubectlInstaller, OSArtifacts, secure_path
from vibe_core.cli.wrappers import AzureCliWrapper


def test_get_storage_account_key_accepts_wrapped_azure_cli_response() -> None:
    az = AzureCliWrapper(OSArtifacts(), "cluster", "resource-group")

    with patch(
        "vibe_core.cli.wrappers.execute_cmd",
        return_value='{"keys": [{"value": "storage-key"}]}',
    ):
        assert az.get_storage_account_key("storage") == "storage-key"


def test_remote_cluster_name_fits_key_vault_limit() -> None:
    assert remote.check_cluster_name_length("a" * 15)
    assert not remote.check_cluster_name_length("a" * 16)


def test_kubectl_minimum_matches_current_aks_version_skew() -> None:
    assert OSArtifacts.REQUIRED_TOOLS["kubectl"].minimum_version == "1.35.0"
    assert KubectlInstaller.KUBECTL_RELEASE_URL.startswith("https://dl.k8s.io/")


def test_refresh_aks_credentials_uses_private_admin_kubeconfig(
    tmp_path: Path,
) -> None:
    artifacts = Mock(spec=OSArtifacts)
    artifacts.az = "az"
    kubeconfig = tmp_path / "kubeconfig"
    artifacts.config_file.return_value = str(kubeconfig)
    az = AzureCliWrapper(artifacts, "cluster", "resource-group")
    az.refresh_az_creds = Mock()
    az.cluster_exists = Mock(return_value=True)
    commands: list[list[str]] = []

    def execute(command: list[str], *args: Any, **kwargs: Any) -> str:
        commands.append(command)
        kubeconfig.touch()
        return ""

    with patch("vibe_core.cli.wrappers.execute_cmd", side_effect=execute):
        az.refresh_aks_credentials()

    assert commands == [
        [
            "az",
            "aks",
            "get-credentials",
            "--admin",
            "--name",
            "cluster",
            "--resource-group",
            "resource-group",
            "--file",
            str(kubeconfig),
            "--overwrite-existing",
        ]
    ]
    assert kubeconfig.stat().st_mode & 0o777 == 0o600


def test_secure_path_uses_owner_only_windows_acl(tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.touch()

    with (
        patch("vibe_core.cli.osartifacts.platform.system", return_value="Windows"),
        patch("vibe_core.cli.osartifacts.subprocess.run") as run,
    ):
        secure_path(path, 0o600)

    command = run.call_args.args[0]
    assert command[:4] == [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
    ]
    assert command[-1] == str(path)
    assert "SetAccessRuleProtection($true, $false)" in command[-2]
    assert run.call_args.kwargs == {
        "check": True,
        "capture_output": True,
        "text": True,
    }
