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
from vibe_core.cli.wrappers import AzureCliWrapper, KubectlWrapper, TerraformWrapper


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


def test_kubectl_uses_baseline_and_target_cluster_skew() -> None:
    assert OSArtifacts.REQUIRED_TOOLS["kubectl"].minimum_version == "1.27.0"
    assert KubectlInstaller.KUBECTL_RELEASE_URL.startswith("https://dl.k8s.io/")
    assert OSArtifacts.kubectl_is_compatible("1.34.9", "1.35.1")
    assert OSArtifacts.kubectl_is_compatible("1.36.0", "1.35.1")
    assert not OSArtifacts.kubectl_is_compatible("1.33.9", "1.35.1")


def test_incompatible_kubectl_installs_target_server_minor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FARMVIBES_AI_CONFIG_DIR", str(tmp_path))
    artifacts = OSArtifacts()
    artifacts._kubectl_path = "/tmp/old-kubectl"
    artifacts.get_version = Mock(side_effect=["1.30.0", "1.35.9"])

    with patch("vibe_core.cli.osartifacts.KubectlInstaller") as installer:
        installer.return_value.cli_name = "kubectl"
        artifacts.ensure_compatible_kubectl("1.35.1")

    installer.assert_called_once_with(tmp_path, "1.35")
    installer.return_value.install.assert_called_once_with()
    assert artifacts.kubectl == str(tmp_path / "kubectl")


def test_get_kubernetes_version_queries_requested_cluster() -> None:
    artifacts = Mock(spec=OSArtifacts)
    artifacts.az = "az"
    az = AzureCliWrapper(artifacts, "cluster", "resource-group")

    with patch(
        "vibe_core.cli.wrappers.execute_cmd", return_value="1.35.1\n"
    ) as execute:
        assert az.get_kubernetes_version() == "1.35.1"

    assert execute.call_args.args[0] == [
        "az",
        "aks",
        "show",
        "--name",
        "cluster",
        "--resource-group",
        "resource-group",
        "--query",
        "kubernetesVersion",
        "-o",
        "tsv",
    ]


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
    on_destroy = Mock()

    with patch("vibe_core.cli.wrappers.KubectlWrapper") as kubectl:
        terraform.destroy_legacy_service_charts(
            "/terraform",
            "state.tfstate",
            {"namespace": "default"},
            "cluster",
            "cluster-admin",
            on_destroy,
        )

    on_destroy.assert_called_once_with()
    terraform.destroy.assert_called_once_with(
        "/terraform",
        "state.tfstate",
        {"namespace": "default"},
        targets=["helm_release.redis", "helm_release.rabbitmq"],
    )
    kubectl.return_value.delete.assert_called_once_with(
        "pvc", "data-rabbitmq-0", ignore_not_found=True
    )


def test_kubectl_ignore_not_found_delete_accepts_empty_output() -> None:
    artifacts = Mock(spec=OSArtifacts)
    artifacts.kubectl = "kubectl"
    kubectl = KubectlWrapper(artifacts, "cluster", config_context="cluster-admin")

    with patch("vibe_core.cli.wrappers.execute_cmd") as execute:
        kubectl.delete("pvc", "missing", ignore_not_found=True)

    assert execute.call_args.args[0] == [
        "kubectl",
        "delete",
        "pvc",
        "missing",
        "--ignore-not-found=true",
    ]
    assert execute.call_args.kwargs["check_empty_result"] is False
