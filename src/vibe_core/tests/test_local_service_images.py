# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json
import os
import socket
import subprocess
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Dict
from unittest.mock import Mock, call

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

        def get_or_none(self, kind: str, name: str):
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


def test_missing_legacy_services_are_not_migration():
    kubectl = Mock(spec=KubectlWrapper)
    kubectl.context.return_value = nullcontext()
    kubectl.get_or_none.return_value = None

    assert local.needs_service_migration(kubectl) is False
    assert kubectl.get_or_none.call_args_list == [
        call("statefulset", "redis-master"),
        call("statefulset", "rabbitmq"),
    ]


def test_service_migration_detection_propagates_kubectl_failures():
    kubectl = Mock(spec=KubectlWrapper)
    kubectl.context.return_value = nullcontext()
    kubectl.get_or_none.side_effect = ValueError("Unable to get statefulset redis-master")

    with pytest.raises(ValueError):
        local.needs_service_migration(kubectl)


def test_kubectl_get_or_none_only_ignores_not_found(
    monkeypatch: pytest.MonkeyPatch,
):
    artifacts = Mock(spec=OSArtifacts)
    artifacts.kubectl = "kubectl"
    commands = []

    def missing(command: Any, **kwargs: Any) -> str:
        commands.append(command)
        return ""

    monkeypatch.setattr(wrappers, "execute_cmd", missing)
    kubectl = KubectlWrapper(artifacts, "test")
    assert kubectl.get_or_none("statefulset", "redis-master") is None
    assert commands == [
        [
            "kubectl",
            "get",
            "statefulset",
            "redis-master",
            "-o",
            "json",
            "--ignore-not-found",
        ]
    ]

    monkeypatch.setattr(
        wrappers,
        "execute_cmd",
        Mock(side_effect=ValueError("Unable to get statefulset redis-master")),
    )
    with pytest.raises(ValueError):
        kubectl.get_or_none("statefulset", "redis-master")


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
    assert kubectl.method_calls == [
        call.context(),
        call.scale("StatefulSet", "redis-master", 0),
        call.wait_for_delete("pod", "redis-master-0", timeout_s=300),
        call.create_redis_volume_pod(redis_image=CUSTOM_REDIS_IMAGE),
        call.cp(str(tmp_path / local.REDIS_DUMP), "redisvolpod:/mnt/dump.rdb"),
        call.delete("pod", "redisvolpod", ignore_not_found=True),
        call.scale("StatefulSet", "redis-master", 1),
        call.rollout_status("statefulset", "redis-master", timeout_s=600),
    ]


def test_restore_redis_data_fails_until_redis_is_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    kubectl = Mock(spec=KubectlWrapper)
    kubectl.context.return_value = nullcontext()
    kubectl.create_redis_volume_pod.return_value = True
    kubectl.rollout_status.side_effect = ValueError("Redis failed to load dump.rdb")
    (tmp_path / local.REDIS_DUMP).write_bytes(b"corrupt")
    monkeypatch.setattr(
        local,
        "find_redis_master",
        lambda kubectl: ("redis-master-0", "redis-master", "StatefulSet"),
    )

    assert not local.restore_redis_data(
        kubectl,
        str(tmp_path),
        skip_confirmation=True,
        redis_image=CUSTOM_REDIS_IMAGE,
    )
    kubectl.delete.assert_called_once_with(
        "pod", "redisvolpod", ignore_not_found=True
    )
    kubectl.scale.assert_called_with("StatefulSet", "redis-master", 1)
    kubectl.rollout_status.assert_called_once_with(
        "statefulset", "redis-master", timeout_s=600
    )


def test_k3d_cluster_config_reads_live_topology(
    monkeypatch: pytest.MonkeyPatch,
):
    cluster = {
        "name": "test",
        "serversCount": 2,
        "agentsCount": 1,
        "nodes": [
            {
                "role": "loadbalancer",
                "portMappings": {
                    "80/tcp": [{"HostIp": "127.0.0.2", "HostPort": "32108"}]
                },
            }
        ],
    }
    registries = [
        {
            "runtimeLabels": {"k3d.cluster": "test"},
            "portMappings": {
                "5000/tcp": [{"HostIp": "127.0.0.2", "HostPort": "5500"}]
            },
        }
    ]
    outputs = iter((json.dumps([cluster]), json.dumps(registries)))
    monkeypatch.setattr(wrappers, "execute_cmd", lambda *args, **kwargs: next(outputs))
    artifacts = Mock(spec=OSArtifacts)
    artifacts.k3d = "k3d"

    assert K3dWrapper(artifacts, "test").get_cluster_config() == {
        "servers": 2,
        "agents": 1,
        "port": 32108,
        "host": "127.0.0.2",
        "registry_port": 5500,
    }


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
    preserved_config = {
        "servers": 2,
        "agents": 1,
        "port": 32108,
        "host": "127.0.0.2",
        "registry_port": 5500,
    }
    k3d.get_cluster_config.return_value = preserved_config
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
    if is_update:
        k3d.create.assert_called_once_with(
            preserved_config["servers"],
            preserved_config["agents"],
            str(tmp_path),
            preserved_config["registry_port"],
            preserved_config["port"],
            preserved_config["host"],
        )


def test_failed_restore_is_retried_by_next_update(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("FARMVIBES_AI_CONFIG_DIR", str(tmp_path / "config"))
    artifacts = Mock(spec=OSArtifacts)
    artifacts.config_dir = tmp_path / "config"
    artifacts.config_dir.mkdir()
    k3d = Mock(spec=K3dWrapper)
    k3d.cluster_name = "test"
    k3d.os_artifacts = artifacts
    k3d.CONTAINERD_IMAGE_PATH = "/images"
    k3d.cluster_exists.return_value = True
    k3d.create.return_value = True
    preserved_config = {
        "servers": 2,
        "agents": 1,
        "port": 32108,
        "host": "127.0.0.2",
        "registry_port": 5500,
    }
    k3d.get_cluster_config.return_value = preserved_config
    kubectl = Mock(spec=KubectlWrapper)
    kubectl.os_artifacts = artifacts
    kubectl.cluster_name = "test"
    kubectl.context_name = "k3d-test"
    terraform = Mock(spec=TerraformWrapper)
    terraform.workspace.return_value = nullcontext()
    terraform.getuid.return_value = 1000
    terraform.getgid.return_value = 1000
    dapr = Mock()
    dapr.needs_upgrade.return_value = False
    restore = Mock(side_effect=(False, True))
    needs_migration = Mock(side_effect=(True, False))
    destroy = Mock(return_value=True)
    monkeypatch.setattr(local, "check_disk_space", lambda path: True)
    monkeypatch.setattr(local, "needs_service_migration", needs_migration)
    monkeypatch.setattr(local, "verify_to_proceed", lambda message: True)
    monkeypatch.setattr(local, "destroy", destroy)
    monkeypatch.setattr(local, "KubectlWrapper", Mock(return_value=kubectl))
    monkeypatch.setattr(local, "DaprWrapper", Mock(return_value=dapr))
    monkeypatch.setattr(local, "TerraformWrapper", Mock(return_value=terraform))
    monkeypatch.setattr(local, "DockerWrapper", Mock(return_value=Mock()))
    monkeypatch.setattr(local, "restore_redis_data", restore)
    monkeypatch.setattr(local, "status", Mock())
    data_path = tmp_path / "data"

    with pytest.raises(RuntimeError, match="Unable to restore Redis workflow state"):
        local.setup(
            k3d,
            storage_path=str(tmp_path),
            data_path=str(data_path),
            worker_replicas=1,
            is_update=True,
            redis_image=CUSTOM_REDIS_IMAGE,
        )
    state_path = data_path / local.REDIS_MIGRATION_STATE
    assert json.loads(state_path.read_text()) == {
        "cluster_name": "test",
        **preserved_config,
    }

    assert local.setup(
        k3d,
        storage_path=str(tmp_path),
        data_path=str(data_path),
        worker_replicas=1,
        is_update=True,
        redis_image=CUSTOM_REDIS_IMAGE,
    )
    assert not state_path.exists()
    destroy.assert_called_once()
    k3d.create.assert_called_once_with(
        preserved_config["servers"],
        preserved_config["agents"],
        str(tmp_path),
        preserved_config["registry_port"],
        preserved_config["port"],
        preserved_config["host"],
    )
    assert restore.call_args_list == [
        call(
            kubectl,
            str(data_path),
            skip_confirmation=True,
            redis_image=CUSTOM_REDIS_IMAGE,
        ),
        call(
            kubectl,
            str(data_path),
            skip_confirmation=True,
            redis_image=CUSTOM_REDIS_IMAGE,
        ),
    ]


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
    outputs = (terraform_dir / "outputs.tf").read_text()
    resources = redis + rabbitmq

    assert "helm_release" not in resources
    assert "bitnami" not in resources.lower()
    assert 'name      = "redis-master"' in redis
    assert 'name      = "redis"' in redis
    assert "redis-password" in redis
    assert "image             = var.redis_image" in redis
    assert 'mount_path = "/data"' in redis
    assert 'image_pull_secrets {\n          name = "acrtoken"\n        }' in redis
    assert (
        'name = "REDISCLI_AUTH"\n\n'
        "            value_from {\n"
        "              secret_key_ref {\n"
        "                name = kubernetes_secret.redis.metadata[0].name\n"
        '                key  = "redis-password"'
    ) in redis
    readiness = redis.split("readiness_probe {", 1)[1].split("liveness_probe {", 1)[0]
    assert 'command = ["sh", "-c", "test \\"$(redis-cli ping)\\" = PONG"]' in readiness
    assert "tcp_socket" not in readiness
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
    assert "kubectl_manifest.cache-initialization-resiliency" in outputs


@pytest.mark.parametrize(
    ("reply", "expected_ready"),
    [
        (b"-LOADING Redis is loading the dataset in memory\r\n", False),
        (b"+PONG\r\n", True),
    ],
)
def test_redis_readiness_requires_pong(tmp_path: Path, reply: bytes, expected_ready: bool):
    redis_cli = tmp_path / "redis-cli"
    redis_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import socket\n"
        "import sys\n"
        'assert os.environ["REDISCLI_AUTH"] == "probe-secret"\n'
        'assert sys.argv[1:] == ["ping"]\n'
        'with socket.create_connection(("127.0.0.1", '
        'int(os.environ["FAKE_REDIS_PORT"]))) as client:\n'
        '    client.sendall(b"PING")\n'
        "    reply = client.recv(4096).decode().strip()\n"
        'print(reply[1:] if reply[:1] in "+-" else reply)\n'
    )
    redis_cli.chmod(0o755)

    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        server.settimeout(5)
        env = {
            **os.environ,
            "FAKE_REDIS_PORT": str(server.getsockname()[1]),
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "REDISCLI_AUTH": "probe-secret",
        }
        probe_command = ["sh", "-c", 'test "$(redis-cli ping)" = PONG']
        assert "probe-secret" not in " ".join(probe_command)
        process = subprocess.Popen(
            probe_command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            connection, _ = server.accept()
            with connection:
                assert connection.recv(4) == b"PING"
                connection.sendall(reply)
            stdout, stderr = process.communicate(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
    assert (process.returncode == 0) is expected_ready
    assert stdout == stderr == ""
