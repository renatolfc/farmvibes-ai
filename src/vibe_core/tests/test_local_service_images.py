# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from contextlib import nullcontext
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import Mock

import pytest

import vibe_core.cli.local as local
import vibe_core.cli.wrappers as wrappers
from vibe_core.cli.constants import DEFAULT_REGISTRY_PATH, RABBITMQ_IMAGE, REDIS_IMAGE
from vibe_core.cli.osartifacts import OSArtifacts
from vibe_core.cli.parsers import LocalCliParser
from vibe_core.cli.wrappers import K3dWrapper, KubectlWrapper, TerraformWrapper

CUSTOM_REDIS_IMAGE = (
    "registry.airgap.example/farmvibes/redis@sha256:"
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
)
CUSTOM_RABBITMQ_IMAGE = (
    "registry.airgap.example/farmvibes/rabbitmq@sha256:"
    "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
)


def test_local_parser_service_image_defaults_and_overrides():
    parser = LocalCliParser("local")

    for action in ("setup", "update"):
        defaults = parser.parse([action])
        assert defaults.redis_image == REDIS_IMAGE
        assert defaults.rabbitmq_image == RABBITMQ_IMAGE

        overrides = parser.parse(
            [
                action,
                "--redis-image",
                CUSTOM_REDIS_IMAGE,
                "--rabbitmq-image",
                CUSTOM_RABBITMQ_IMAGE,
            ]
        )
        assert overrides.redis_image == CUSTOM_REDIS_IMAGE
        assert overrides.rabbitmq_image == CUSTOM_RABBITMQ_IMAGE


@pytest.mark.parametrize(("action", "is_update"), [("setup", False), ("update", True)])
def test_local_dispatch_forwards_service_images(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, action: str, is_update: bool
):
    class K3d:
        def __init__(self, os_artifacts: OSArtifacts, cluster_name: str):
            self.os_artifacts = os_artifacts
            self.cluster_name = cluster_name

        @staticmethod
        def cluster_exists() -> bool:
            return False

    captured: Dict[str, Any] = {}

    def capture_setup(*args: Any, **kwargs: Any) -> bool:
        captured.update(kwargs)
        return True

    monkeypatch.setenv("FARMVIBES_AI_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(OSArtifacts, "check_dependencies", lambda *args, **kwargs: None)
    monkeypatch.setattr(local, "K3dWrapper", K3d)
    monkeypatch.setattr(local, "setup", capture_setup)

    args = LocalCliParser("local").parse(
        [
            action,
            "--storage-path",
            str(tmp_path),
            "--redis-image",
            CUSTOM_REDIS_IMAGE,
            "--rabbitmq-image",
            CUSTOM_RABBITMQ_IMAGE,
        ]
    )
    assert local.dispatch(args) is True
    assert captured["redis_image"] == CUSTOM_REDIS_IMAGE
    assert captured["rabbitmq_image"] == CUSTOM_RABBITMQ_IMAGE
    assert captured["is_update"] is is_update


def test_terraform_wrapper_propagates_service_images(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    artifacts = OSArtifacts()
    artifacts._local_terraform_path = str(tmp_path)
    monkeypatch.setattr(
        artifacts,
        "get_terraform_file",
        lambda file_name, *args: str(tmp_path / file_name),
    )
    terraform = TerraformWrapper.__new__(TerraformWrapper)
    terraform.os_artifacts = artifacts
    terraform.environment = "public"
    terraform.az = None
    captured: Dict[str, Any] = {}

    monkeypatch.setattr(terraform, "init", lambda *args, **kwargs: None)
    monkeypatch.setattr(terraform, "getuid", lambda: 1000)
    monkeypatch.setattr(terraform, "getgid", lambda: 1000)
    monkeypatch.setattr(
        terraform,
        "apply",
        lambda directory, state, variables, **kwargs: captured.update(variables),
    )
    monkeypatch.setattr(terraform, "get_output", lambda *args, **kwargs: {})

    terraform.ensure_local_cluster(
        cluster_name="test",
        registry="registry.example",
        log_level="INFO",
        max_log_file_bytes=None,
        log_backup_count=None,
        image_tag="test",
        image_prefix="",
        data_path=str(tmp_path),
        worker_replicas=1,
        config_context="k3d-test",
        enable_telemetry=False,
        redis_image=CUSTOM_REDIS_IMAGE,
        rabbitmq_image=CUSTOM_RABBITMQ_IMAGE,
    )

    assert captured["redis_image"] == CUSTOM_REDIS_IMAGE
    assert captured["rabbitmq_image"] == CUSTOM_RABBITMQ_IMAGE
    assert "redis_image_tag" not in captured
    assert "rabbitmq_image_tag" not in captured


def test_legacy_chart_services_require_migration():
    class Kubectl(KubectlWrapper):
        def context(self, cluster_name: str = ""):
            return nullcontext()

        def get(self, kind: str, name: str, jsonpath: Optional[str] = None):
            assert kind == "statefulset"
            return {
                "metadata": {
                    "labels": {
                        "app.kubernetes.io/managed-by": (
                            "Helm" if name == "rabbitmq" else "Terraform"
                        )
                    }
                }
            }

    assert local.needs_service_migration(Kubectl.__new__(Kubectl)) is True


def test_restore_redis_data_forwards_selected_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    kubectl = Mock(spec=KubectlWrapper)
    kubectl.context.return_value = nullcontext()
    kubectl.create_redis_volume_pod.return_value = True
    (tmp_path / local.REDIS_DUMP).touch()
    monkeypatch.setattr(
        local,
        "find_redis_master",
        lambda kubectl: ("redis-master-0", "redis-master", "StatefulSet"),
    )

    assert local.restore_redis_data(
        kubectl,
        str(tmp_path),
        skip_confirmation=True,
        redis_image=CUSTOM_REDIS_IMAGE,
    )
    kubectl.create_redis_volume_pod.assert_called_once_with(redis_image=CUSTOM_REDIS_IMAGE)


@pytest.mark.parametrize(
    (
        "is_update",
        "cluster_exists",
        "redis_image",
        "rabbitmq_image",
        "registry",
        "username",
        "password",
    ),
    [
        (
            False,
            False,
            REDIS_IMAGE,
            RABBITMQ_IMAGE,
            DEFAULT_REGISTRY_PATH,
            "",
            "",
        ),
        (
            True,
            True,
            CUSTOM_REDIS_IMAGE,
            CUSTOM_RABBITMQ_IMAGE,
            "registry.airgap.example",
            "robot",
            "token",
        ),
    ],
)
def test_setup_uses_service_images_for_workloads_and_restore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    is_update: bool,
    cluster_exists: bool,
    redis_image: str,
    rabbitmq_image: str,
    registry: str,
    username: str,
    password: str,
):
    monkeypatch.setenv("FARMVIBES_AI_CONFIG_DIR", str(tmp_path / "config"))
    artifacts = Mock(spec=OSArtifacts)
    artifacts.config_dir = tmp_path / "config"
    artifacts.config_dir.mkdir()
    k3d = Mock(spec=K3dWrapper)
    k3d.cluster_name = "test"
    k3d.os_artifacts = artifacts
    k3d.CONTAINERD_IMAGE_PATH = "/images"
    k3d.cluster_exists.return_value = cluster_exists
    k3d.create.return_value = True
    kubectl = Mock(spec=KubectlWrapper)
    kubectl.os_artifacts = artifacts
    kubectl.cluster_name = "test"
    kubectl.context_name = "k3d-test"
    terraform = Mock(spec=TerraformWrapper)
    terraform.workspace.return_value = nullcontext()
    terraform.getuid.return_value = 1000
    terraform.getgid.return_value = 1000
    restore = Mock(return_value=True)
    monkeypatch.setattr(local, "check_disk_space", lambda path: True)
    monkeypatch.setattr(local, "needs_service_migration", lambda kubectl: is_update)
    monkeypatch.setattr(local, "verify_to_proceed", lambda message: True)
    monkeypatch.setattr(local, "destroy", Mock(return_value=True))
    monkeypatch.setattr(local, "KubectlWrapper", Mock(return_value=kubectl))
    monkeypatch.setattr(local, "DaprWrapper", Mock())
    monkeypatch.setattr(local, "TerraformWrapper", Mock(return_value=terraform))
    monkeypatch.setattr(local, "DockerWrapper", Mock(return_value=Mock()))
    monkeypatch.setattr(local, "restore_redis_data", restore)
    monkeypatch.setattr(local, "status", Mock())

    assert local.setup(
        k3d,
        storage_path=str(tmp_path),
        data_path=str(tmp_path / "data"),
        worker_replicas=1,
        is_update=is_update,
        registry=registry,
        username=username,
        password=password,
        redis_image=redis_image,
        rabbitmq_image=rabbitmq_image,
    )
    assert terraform.ensure_local_cluster.call_args.args[11] == redis_image
    assert terraform.ensure_local_cluster.call_args.args[12] == rabbitmq_image
    restore.assert_called_once_with(
        kubectl,
        str(tmp_path / "data"),
        skip_confirmation=is_update,
        redis_image=redis_image,
    )
    if password:
        kubectl.create_docker_token.assert_called_once_with(
            "acrtoken", registry, username, password
        )
    else:
        kubectl.create_docker_token.assert_not_called()


def test_redis_volume_pod_renders_default_and_selected_images(
    monkeypatch: pytest.MonkeyPatch,
):
    manifests = []
    artifacts = Mock(spec=OSArtifacts)
    artifacts.kubectl = "kubectl"

    def capture_manifest(command: Any, **kwargs: Any) -> str:
        if command[1:3] == ["apply", "-f"]:
            manifests.append(Path(command[3]).read_text())
        return ""

    monkeypatch.setattr(wrappers, "execute_cmd", capture_manifest)
    kubectl = KubectlWrapper(artifacts, "test")

    assert kubectl.create_redis_volume_pod()
    assert kubectl.create_redis_volume_pod(redis_image=CUSTOM_REDIS_IMAGE)
    assert f"image: {REDIS_IMAGE}" in manifests[0]
    assert f"image: {CUSTOM_REDIS_IMAGE}" in manifests[1]
    assert all("imagePullSecrets:\n  - name: acrtoken" in manifest for manifest in manifests)


def test_native_terraform_preserves_service_contracts():
    terraform_dir = (
        Path(__file__).parents[1] / "vibe_core" / "terraform" / "local" / "modules" / "kubernetes"
    )
    redis = (terraform_dir / "redis.tf").read_text()
    rabbitmq = (terraform_dir / "rabbitmq.tf").read_text()
    dapr = (terraform_dir / "dapr.tf").read_text()
    resources = redis + rabbitmq

    assert "helm_release" not in resources
    assert "bitnami" not in resources.lower()
    assert 'name      = "redis-master"' in redis
    assert 'name      = "redis"' in redis
    assert "redis-password" in redis
    assert "image             = var.redis_image" in redis
    assert 'mount_path = "/data"' in redis
    assert 'image_pull_secrets {\n          name = "acrtoken"\n        }' in redis
    assert 'name      = "rabbitmq"' in rabbitmq
    assert "rabbitmq-password" in rabbitmq
    assert "image             = var.rabbitmq_image" in rabbitmq
    assert 'value = "user"' in rabbitmq
    assert 'mount_path = "/var/lib/rabbitmq"' in rabbitmq
    assert 'image_pull_secrets {\n          name = "acrtoken"\n        }' in rabbitmq
    assert "value: redis-master:6379" in dapr
    assert "key: redis-password" in dapr
    assert "key: rabbitmq-password" in dapr
    assert (
        "DaprBuiltInInitializationRetries:\n"
        "            policy: constant\n"
        "            duration: 1s\n"
        "            maxRetries: 15"
    ) in dapr
    assert "name: cache-initialization-resiliency" in dapr
    assert "scopes:\n      - terravibes-cache" in dapr
    assert dapr.count("DaprBuiltInInitializationRetries:") == 2
    assert "maxRetries: 15\n      targets: {}" in dapr
