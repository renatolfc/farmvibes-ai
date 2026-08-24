# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from pathlib import Path
from typing import Any, List
from unittest.mock import Mock, patch

import pytest

from vibe_core.cli import remote
from vibe_core.cli.osartifacts import (
    KubectlInstaller,
    OSArtifacts,
    github_api_headers,
    secure_path,
)
from vibe_core.cli.wrappers import AzureCliWrapper, TerraformWrapper


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


def test_kubectl_does_not_replace_clients_for_unrelated_cluster_versions() -> None:
    assert OSArtifacts.REQUIRED_TOOLS["kubectl"].minimum_version is None
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
    commands: List[List[str]] = []

    def execute(command: List[str], *args: Any, **kwargs: Any) -> str:
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


def test_secure_path_uses_windows_acl_for_wsl_mount() -> None:
    path = Path("/mnt/c/shared/token")

    with (
        patch("vibe_core.cli.osartifacts.platform.system", return_value="Linux"),
        patch("vibe_core.cli.osartifacts.in_wsl", return_value=True),
        patch(
            "vibe_core.cli.osartifacts.subprocess.check_output",
            return_value="C:\\shared\\token\n",
        ) as check_output,
        patch("vibe_core.cli.osartifacts.subprocess.run") as run,
    ):
        secure_path(path, 0o600)

    check_output.assert_called_once_with(
        ["wslpath", "-w", str(path)],
        text=True,
    )
    assert run.call_args.args[0][-1] == "C:\\shared\\token"


def test_github_api_headers_use_actions_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert github_api_headers() == {}

    monkeypatch.setenv("GITHUB_TOKEN", "actions-token")
    assert github_api_headers() == {"Authorization": "Bearer actions-token"}


def test_config_directory_is_owner_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "config"
    path.mkdir(mode=0o777)
    monkeypatch.setenv("FARMVIBES_AI_CONFIG_DIR", str(path))

    assert OSArtifacts().config_dir == path
    assert path.stat().st_mode & 0o777 == 0o700


def test_legacy_service_charts_are_destroyed_before_rabbitmq_pvc_reset() -> None:
    artifacts = Mock(spec=OSArtifacts)
    terraform = TerraformWrapper(artifacts)
    terraform.state_resources = Mock(
        return_value=["helm_release.redis", "helm_release.rabbitmq"]
    )
    terraform.destroy = Mock()

    with patch("vibe_core.cli.wrappers.KubectlWrapper") as kubectl:
        terraform.destroy_legacy_service_charts(
            "/terraform",
            "state.tfstate",
            {"namespace": "default"},
            "cluster",
            "cluster-admin",
        )

    terraform.destroy.assert_called_once_with(
        "/terraform",
        "state.tfstate",
        {"namespace": "default"},
        targets=["helm_release.redis", "helm_release.rabbitmq"],
    )
    kubectl.return_value.delete.assert_called_once_with(
        "pvc", "data-rabbitmq-0", ignore_not_found=True
    )
