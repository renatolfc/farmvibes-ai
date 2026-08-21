# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import base64
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

    setup_defaults = parser.parse(["setup"])
    assert setup_defaults.redis_image == REDIS_IMAGE
    assert setup_defaults.rabbitmq_image == RABBITMQ_IMAGE

    update_defaults = parser.parse(["update"])
    for field in (
        "servers",
        "agents",
        "registry",
        "image_tag",
        "image_prefix",
        "redis_image",
        "rabbitmq_image",
        "log_level",
        "worker_replicas",
        "port",
        "host",
        "registry_port",
        "enable_telemetry",
    ):
        assert getattr(update_defaults, field) is None

    for action in ("setup", "update"):
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


def test_destroy_dispatch_uses_saved_storage_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    class K3d:
        def __init__(self, os_artifacts: OSArtifacts, cluster_name: str):
            self.os_artifacts = os_artifacts
            self.cluster_name = cluster_name

    config_dir = tmp_path / "config"
    storage_path = tmp_path / "custom-storage"
    config_dir.mkdir()
    (config_dir / "storage").write_text(str(tmp_path / "other-cluster-storage"))
    (
        config_dir / local.LOCAL_CONFIG.format(cluster_name="test")
    ).write_text(json.dumps({"storage_path": str(storage_path)}))
    captured: Dict[str, Any] = {}

    def capture_destroy(k3d: K3d, **kwargs: Any) -> bool:
        captured.update(kwargs)
        return True

    monkeypatch.setenv("FARMVIBES_AI_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(OSArtifacts, "check_dependencies", lambda *args, **kwargs: None)
    monkeypatch.setattr(local, "K3dWrapper", K3d)
    monkeypatch.setattr(local, "destroy", capture_destroy)

    args = LocalCliParser("local").parse(
        ["destroy", "--cluster-name", "test"]
    )
    assert local.dispatch(args)
    assert captured["data_path"] == str(storage_path / local.DATA_SUFFIX)


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


def test_effective_config_is_reconstructed_from_cluster():
    k3d = Mock(spec=K3dWrapper)
    k3d.get_cluster_config.return_value = {
        "servers": 2,
        "agents": 1,
        "port": 32108,
        "host": "127.0.0.2",
        "registry_port": 5500,
    }
    kubectl = Mock(spec=KubectlWrapper)
    kubectl.context.return_value = nullcontext()
    resources = {
        ("deployment", "terravibes-worker"): {
            "spec": {
                "replicas": 3,
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "image": (
                                    "registry.example:5443/team/farmvibes/worker:v42"
                                ),
                                "args": [
                                    "worker.impl.loglevel=INFO",
                                    "worker.impl.max_log_file_bytes=123456",
                                    "worker.impl.log_backup_count=2",
                                ],
                            }
                        ]
                    }
                },
            }
        },
        ("statefulset", "redis-master"): {
            "metadata": {"labels": {}},
            "spec": {
                "template": {
                    "spec": {"containers": [{"image": CUSTOM_REDIS_IMAGE}]}
                }
            },
        },
        ("statefulset", "rabbitmq"): {
            "metadata": {"labels": {}},
            "spec": {
                "template": {
                    "spec": {"containers": [{"image": CUSTOM_RABBITMQ_IMAGE}]}
                }
            },
        },
        ("deployment", "otel-collector"): {"metadata": {"name": "otel-collector"}},
    }
    kubectl.get_or_none.side_effect = lambda kind, name: resources.get((kind, name))

    assert local.inspect_effective_config(k3d, kubectl) == {
        "servers": 2,
        "agents": 1,
        "port": 32108,
        "host": "127.0.0.2",
        "registry_port": 5500,
        "registry": "registry.example:5443",
        "image_prefix": "team/farmvibes/",
        "image_tag": "v42",
        "worker_replicas": 3,
        "enable_telemetry": True,
        "log_level": "INFO",
        "max_log_file_bytes": 123456,
        "log_backup_count": 2,
        "redis_image": CUSTOM_REDIS_IMAGE,
        "rabbitmq_image": CUSTOM_RABBITMQ_IMAGE,
    }


def test_restore_marker_probe_propagates_transient_failures(
    monkeypatch: pytest.MonkeyPatch,
):
    kubectl = Mock(spec=KubectlWrapper)
    kubectl.context.return_value = nullcontext()
    kubectl.exec.side_effect = ValueError("Kubernetes API unavailable")
    monkeypatch.setattr(
        local,
        "find_redis_master",
        lambda kubectl: ("redis-master-0", "redis-master", "StatefulSet"),
    )

    with pytest.raises(ValueError, match="Kubernetes API unavailable"):
        local.redis_migration_marker_matches(
            kubectl,
            {"marker_key": "migration-key", "marker_value": "migration-value"},
        )


def test_restore_marker_uses_container_rediscli_auth(
    monkeypatch: pytest.MonkeyPatch,
):
    kubectl = Mock(spec=KubectlWrapper)
    kubectl.context.return_value = nullcontext()
    kubectl.exec.return_value = "migration-value"
    state = {
        "marker_key": "migration-key",
        "marker_value": "migration-value",
    }
    monkeypatch.setattr(
        local,
        "find_redis_master",
        lambda kubectl: ("redis-master-0", "redis-master", "StatefulSet"),
    )

    assert local.redis_migration_marker_matches(kubectl, state)
    local.clear_redis_migration_marker(kubectl, state)
    commands = [invocation.args[1] for invocation in kubectl.exec.call_args_list]
    assert commands == [
        ["sh", "-c", 'redis-cli --raw GET "$1"', "sh", "migration-key"],
        ["sh", "-c", 'redis-cli DEL "$1" >/dev/null', "sh", "migration-key"],
    ]
    assert kubectl.exec.call_args_list[1].kwargs == {
        "capture_output": False,
        "censor_command": True,
    }
    assert all("REDIS_PASSWORD" not in " ".join(command) for command in commands)


def test_k3d_does_not_share_containerd_store_across_nodes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    configs = []
    artifacts = Mock(spec=OSArtifacts)
    artifacts.k3d = "k3d"

    def capture_config(command: Any, **kwargs: Any) -> str:
        configs.append(Path(command[-1]).read_text())
        return ""

    monkeypatch.setattr(wrappers, "is_port_free", lambda port: True)
    monkeypatch.setattr(wrappers, "execute_cmd", capture_config)
    k3d = K3dWrapper(artifacts, "test")

    assert k3d.create(1, 1, str(tmp_path), 5000, 31108, "127.0.0.1")
    containerd_section = configs[0].split(
        f"{tmp_path}/registry:{K3dWrapper.CONTAINERD_IMAGE_PATH}", 1
    )[1].split("registries:", 1)[0]
    assert "server:0" in containerd_section
    assert "server:*" not in containerd_section
    assert "agent:*" not in containerd_section
    assert "agents: 1" in configs[0]


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
    kubectl.context.return_value = nullcontext()
    terraform = Mock(spec=TerraformWrapper)
    terraform.workspace.return_value = nullcontext()
    terraform.getuid.return_value = 1000
    terraform.getgid.return_value = 1000
    restore = Mock(return_value=True)

    def backup(
        kubectl: KubectlWrapper,
        data_path: str,
        dump_file: str,
        **kwargs: Any,
    ) -> bool:
        Path(data_path, dump_file).write_bytes(b"migration-rdb")
        return True

    monkeypatch.setattr(local, "check_disk_space", lambda path: True)
    monkeypatch.setattr(
        local,
        "inspect_effective_config",
        lambda *args: preserved_config,
    )
    monkeypatch.setattr(local, "needs_service_migration", lambda kubectl: is_update)
    monkeypatch.setattr(local, "verify_to_proceed", lambda message: True)
    monkeypatch.setattr(local, "destroy", Mock(return_value=True))
    monkeypatch.setattr(local, "get_pull_secret", lambda kubectl: "e30=")
    monkeypatch.setattr(local, "backup_redis_data", backup)
    monkeypatch.setattr(local, "KubectlWrapper", Mock(return_value=kubectl))
    monkeypatch.setattr(local, "DaprWrapper", Mock())
    monkeypatch.setattr(local, "TerraformWrapper", Mock(return_value=terraform))
    monkeypatch.setattr(local, "DockerWrapper", Mock(return_value=Mock()))
    monkeypatch.setattr(local, "restore_redis_data", restore)
    monkeypatch.setattr(local, "redis_migration_marker_matches", lambda *args: True)
    monkeypatch.setattr(local, "clear_redis_migration_marker", Mock())
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
    assert restore.call_args.args == (kubectl, str(tmp_path / "data"))
    assert restore.call_args.kwargs["skip_confirmation"] is is_update
    assert restore.call_args.kwargs["redis_image"] == redis_image
    if is_update:
        assert restore.call_args.kwargs["dump_file"].startswith("redis-migration-")
    if password:
        assert kubectl.create_docker_token.call_args_list == [
            call("acrtoken", registry, username, password),
            call("acrtoken", registry, username, password),
        ]
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

    def backup(
        kubectl: KubectlWrapper,
        data_path: str,
        dump_file: str,
        **kwargs: Any,
    ) -> bool:
        Path(data_path, dump_file).write_bytes(b"migration-rdb")
        return True

    monkeypatch.setattr(local, "check_disk_space", lambda path: True)
    monkeypatch.setattr(
        local,
        "inspect_effective_config",
        lambda *args: preserved_config,
    )
    monkeypatch.setattr(local, "needs_service_migration", needs_migration)
    monkeypatch.setattr(local, "verify_to_proceed", lambda message: True)
    monkeypatch.setattr(local, "destroy", destroy)
    monkeypatch.setattr(local, "get_pull_secret", lambda kubectl: None)
    monkeypatch.setattr(local, "backup_redis_data", backup)
    monkeypatch.setattr(local, "KubectlWrapper", Mock(return_value=kubectl))
    monkeypatch.setattr(local, "DaprWrapper", Mock(return_value=dapr))
    monkeypatch.setattr(local, "TerraformWrapper", Mock(return_value=terraform))
    monkeypatch.setattr(local, "DockerWrapper", Mock(return_value=Mock()))
    monkeypatch.setattr(local, "restore_redis_data", restore)
    marker_matches = Mock(side_effect=(False, True))
    monkeypatch.setattr(local, "redis_migration_marker_matches", marker_matches)
    monkeypatch.setattr(local, "clear_redis_migration_marker", Mock())
    monkeypatch.setattr(local, "status", Mock())
    data_path = tmp_path / "data"

    with pytest.raises(RuntimeError, match="Unable to verify Redis workflow state"):
        local.setup(
            k3d,
            storage_path=str(tmp_path),
            data_path=str(data_path),
            worker_replicas=1,
            is_update=True,
            redis_image=CUSTOM_REDIS_IMAGE,
        )
    state_path = data_path / local.REDIS_MIGRATION_STATE
    first_state = json.loads(state_path.read_text())
    assert first_state["phase"] == "restoring"
    assert first_state["backup_sha256"]

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
    assert restore.call_count == 2
    assert {
        invocation.kwargs["dump_file"] for invocation in restore.call_args_list
    } == {first_state["backup_file"]}
    assert all(
        invocation.kwargs["redis_image"] == CUSTOM_REDIS_IMAGE
        for invocation in restore.call_args_list
    )


def effective_config(tmp_path: Path) -> Dict[str, Any]:
    return {
        "servers": 2,
        "agents": 1,
        "storage_path": str(tmp_path),
        "registry": "registry.airgap.example/team",
        "log_level": "INFO",
        "max_log_file_bytes": 123456,
        "log_backup_count": 2,
        "image_tag": "v42",
        "image_prefix": "farmvibes/",
        "worker_replicas": 3,
        "enable_telemetry": True,
        "port": 32108,
        "host": "127.0.0.2",
        "registry_port": 5500,
        "redis_image": CUSTOM_REDIS_IMAGE,
        "rabbitmq_image": CUSTOM_RABBITMQ_IMAGE,
    }


def setup_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cluster_exists: bool = True,
):
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
    kubectl.context.return_value = nullcontext()
    terraform = Mock(spec=TerraformWrapper)
    terraform.workspace.return_value = nullcontext()
    terraform.getuid.return_value = 1000
    terraform.getgid.return_value = 1000
    dapr = Mock()
    dapr.needs_upgrade.return_value = False

    monkeypatch.setattr(local, "check_disk_space", lambda path: True)
    monkeypatch.setattr(local, "verify_to_proceed", lambda message: True)
    monkeypatch.setattr(local, "KubectlWrapper", Mock(return_value=kubectl))
    monkeypatch.setattr(local, "DaprWrapper", Mock(return_value=dapr))
    monkeypatch.setattr(local, "TerraformWrapper", Mock(return_value=terraform))
    monkeypatch.setattr(local, "DockerWrapper", Mock(return_value=Mock()))
    monkeypatch.setattr(local, "status", Mock())
    return artifacts, k3d, kubectl, terraform


def save_pending_state(
    data_path: Path, config: Dict[str, Any], phase: str
) -> Dict[str, Any]:
    data_path.mkdir(exist_ok=True)
    state = local.new_redis_migration_state("test", config, None)
    backup = data_path / state["backup_file"]
    backup.write_bytes(b"immutable-rdb")
    state["backup_sha256"] = local.file_sha256(str(backup))
    return local.save_redis_migration_state(str(data_path), state, phase)


def test_migration_preserves_complete_config_and_pull_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _, k3d, kubectl, terraform = setup_dependencies(monkeypatch, tmp_path)
    config = effective_config(tmp_path)
    docker_config = base64.b64encode(
        b'{"auths":{"registry.airgap.example":{"auth":"opaque"}}}'
    ).decode()
    restore = Mock(return_value=True)

    def backup(
        kubectl: KubectlWrapper,
        data_path: str,
        dump_file: str,
        **kwargs: Any,
    ) -> bool:
        Path(data_path, dump_file).write_bytes(b"migration-rdb")
        return True

    monkeypatch.setattr(local, "inspect_effective_config", lambda *args: config)
    monkeypatch.setattr(local, "needs_service_migration", lambda kubectl: True)
    monkeypatch.setattr(local, "get_pull_secret", lambda kubectl: docker_config)
    monkeypatch.setattr(local, "backup_redis_data", backup)
    monkeypatch.setattr(local, "destroy", Mock(return_value=True))
    monkeypatch.setattr(local, "restore_redis_data", restore)
    monkeypatch.setattr(local, "redis_migration_marker_matches", lambda *args: True)
    monkeypatch.setattr(local, "clear_redis_migration_marker", Mock())

    assert local.setup(
        k3d,
        storage_path=str(tmp_path),
        data_path=str(tmp_path / "data"),
        is_update=True,
        servers=None,
        agents=None,
        registry=None,
        log_level=None,
        image_tag=None,
        image_prefix=None,
        worker_replicas=None,
        enable_telemetry=None,
        port=None,
        host=None,
        registry_port=None,
        redis_image=None,
        rabbitmq_image=None,
    )
    k3d.create.assert_called_once_with(
        2, 1, str(tmp_path), 5500, 32108, "127.0.0.2"
    )
    ensure = terraform.ensure_local_cluster.call_args
    assert ensure.args[1:7] == (
        "registry.airgap.example/team",
        "INFO",
        123456,
        2,
        "v42",
        "farmvibes/",
    )
    assert ensure.args[8] == 3
    assert ensure.args[10:13] == (
        True,
        CUSTOM_REDIS_IMAGE,
        CUSTOM_RABBITMQ_IMAGE,
    )
    assert ensure.kwargs["is_update"] is False
    kubectl.apply_docker_config_secret.assert_called_once_with(
        "acrtoken", docker_config
    )
    assert restore.call_args.kwargs["dump_file"].startswith("redis-migration-")
    assert restore.call_args.kwargs["redis_image"] == CUSTOM_REDIS_IMAGE
    assert not (tmp_path / "data" / local.REDIS_MIGRATION_STATE).exists()
    assert json.loads(
        (
            tmp_path
            / "config"
            / local.LOCAL_CONFIG.format(cluster_name="test")
        ).read_text()
    ) == config


def test_migration_aborts_before_destroy_when_private_auth_is_unrecoverable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _, k3d, _, _ = setup_dependencies(monkeypatch, tmp_path)
    config = {
        **effective_config(tmp_path),
        "registry": "private.azurecr.io",
    }
    destroy = Mock(return_value=True)
    azure = Mock()
    azure.request_registry_token.return_value = ""
    monkeypatch.setattr(local, "inspect_effective_config", lambda *args: config)
    monkeypatch.setattr(local, "needs_service_migration", lambda kubectl: True)
    monkeypatch.setattr(local, "get_pull_secret", lambda kubectl: None)
    monkeypatch.setattr(local, "AzureCliWrapper", Mock(return_value=azure))
    monkeypatch.setattr(local, "destroy", destroy)
    monkeypatch.setattr(local, "backup_redis_data", Mock())

    with pytest.raises(RuntimeError, match="Unable to recover credentials"):
        local.setup(
            k3d,
            storage_path=str(tmp_path),
            data_path=str(tmp_path / "data"),
            is_update=True,
            worker_replicas=None,
            registry=None,
        )
    destroy.assert_not_called()
    local.backup_redis_data.assert_not_called()


def test_pending_created_cluster_runs_fresh_terraform(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _, k3d, _, terraform = setup_dependencies(monkeypatch, tmp_path)
    config = effective_config(tmp_path)
    save_pending_state(tmp_path / "data", config, "cluster_created")
    restore = Mock(return_value=True)

    monkeypatch.setattr(local, "inspect_effective_config", lambda *args: {})
    monkeypatch.setattr(local, "needs_service_migration", lambda kubectl: False)
    monkeypatch.setattr(local, "restore_redis_data", restore)
    monkeypatch.setattr(local, "redis_migration_marker_matches", lambda *args: True)
    monkeypatch.setattr(local, "clear_redis_migration_marker", Mock())

    assert local.setup(
        k3d,
        storage_path=str(tmp_path),
        data_path=str(tmp_path / "data"),
        is_update=True,
        worker_replicas=None,
        redis_image=None,
        rabbitmq_image=None,
    )
    k3d.create.assert_not_called()
    assert terraform.ensure_local_cluster.call_args.kwargs["is_update"] is False
    assert terraform.ensure_local_cluster.call_args.args[11:13] == (
        CUSTOM_REDIS_IMAGE,
        CUSTOM_RABBITMQ_IMAGE,
    )


def test_corrupt_pending_backup_aborts_before_provisioning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _, k3d, _, terraform = setup_dependencies(monkeypatch, tmp_path)
    config = effective_config(tmp_path)
    state = save_pending_state(tmp_path / "data", config, "backed_up")
    (tmp_path / "data" / state["backup_file"]).write_bytes(b"corrupt")
    monkeypatch.setattr(local, "inspect_effective_config", lambda *args: {})
    monkeypatch.setattr(local, "needs_service_migration", lambda kubectl: False)

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        local.setup(
            k3d,
            storage_path=str(tmp_path),
            data_path=str(tmp_path / "data"),
            is_update=True,
            worker_replicas=None,
        )
    k3d.create.assert_not_called()
    terraform.ensure_local_cluster.assert_not_called()


def test_restored_phase_retry_never_replays_backup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _, k3d, _, terraform = setup_dependencies(monkeypatch, tmp_path)
    config = effective_config(tmp_path)
    save_pending_state(tmp_path / "data", config, "provisioned")
    restore = Mock(return_value=True)
    clear_marker = Mock(
        side_effect=(RuntimeError("injected marker cleanup failure"), None)
    )

    monkeypatch.setattr(local, "inspect_effective_config", lambda *args: {})
    monkeypatch.setattr(local, "needs_service_migration", lambda kubectl: False)
    monkeypatch.setattr(local, "restore_redis_data", restore)
    monkeypatch.setattr(local, "redis_migration_marker_matches", lambda *args: True)
    monkeypatch.setattr(local, "clear_redis_migration_marker", clear_marker)

    setup_args = {
        "storage_path": str(tmp_path),
        "data_path": str(tmp_path / "data"),
        "is_update": True,
        "worker_replicas": None,
        "redis_image": None,
        "rabbitmq_image": None,
    }
    with pytest.raises(RuntimeError, match="injected marker cleanup"):
        local.setup(k3d, **setup_args)
    assert json.loads(
        (tmp_path / "data" / local.REDIS_MIGRATION_STATE).read_text()
    )["phase"] == "restored"

    assert local.setup(k3d, **setup_args)
    assert restore.call_count == 1
    assert clear_marker.call_count == 2
    assert terraform.ensure_local_cluster.call_count == 2
    assert not (tmp_path / "data" / local.REDIS_MIGRATION_STATE).exists()


def test_normal_update_preserves_custom_service_images(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    artifacts, k3d, _, terraform = setup_dependencies(monkeypatch, tmp_path)
    config = effective_config(tmp_path)
    local.save_local_config(artifacts, "test", config)
    live_config = {
        **config,
        "registry": "registry.airgap.example",
        "image_prefix": "team/farmvibes/",
    }
    monkeypatch.setattr(local, "inspect_effective_config", lambda *args: live_config)
    monkeypatch.setattr(local, "needs_service_migration", lambda kubectl: False)

    assert local.setup(
        k3d,
        storage_path=str(tmp_path),
        data_path=str(tmp_path / "data"),
        is_update=True,
        worker_replicas=None,
        redis_image=None,
        rabbitmq_image=None,
    )
    ensure = terraform.ensure_local_cluster.call_args
    assert ensure.args[1] == "registry.airgap.example/team"
    assert ensure.args[6] == "farmvibes/"
    assert ensure.args[11:13] == (CUSTOM_REDIS_IMAGE, CUSTOM_RABBITMQ_IMAGE)
    assert ensure.kwargs["is_update"] is True


def test_migration_backup_is_immutable_checksummed_and_private(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    data_path = tmp_path / "data"
    data_path.mkdir()
    state = local.new_redis_migration_state(
        "test", effective_config(tmp_path), None
    )
    state = local.save_redis_migration_state(str(data_path), state)
    calls = 0

    def backup(
        kubectl: KubectlWrapper,
        data_path: str,
        dump_file: str,
        **kwargs: Any,
    ) -> bool:
        nonlocal calls
        calls += 1
        Path(data_path, dump_file).write_bytes(b"original-rdb")
        return True

    monkeypatch.setattr(local, "backup_redis_data", backup)
    state = local.ensure_migration_backup(Mock(), str(data_path), state)
    backup_path = data_path / state["backup_file"]
    assert state["phase"] == "backed_up"
    assert state["backup_sha256"] == local.file_sha256(str(backup_path))
    assert backup_path.read_bytes() == b"original-rdb"
    assert (data_path / local.REDIS_MIGRATION_STATE).stat().st_mode & 0o777 == 0o600
    assert backup_path.stat().st_mode & 0o777 == 0o600

    local.ensure_migration_backup(Mock(), str(data_path), state)
    assert calls == 1
    assert backup_path.read_bytes() == b"original-rdb"

    backup_path.write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        local.ensure_migration_backup(Mock(), str(data_path), state)


def test_pending_migration_destroy_does_not_replace_backup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    data_path = tmp_path / "data"
    state = save_pending_state(
        data_path, effective_config(tmp_path), "cluster_created"
    )
    backup_path = data_path / state["backup_file"]
    k3d = Mock(spec=K3dWrapper)
    k3d.cluster_name = "test"
    k3d.cluster_exists.side_effect = (True, False)
    k3d.delete.return_value = True
    k3d.os_artifacts = Mock(spec=OSArtifacts)
    terraform = Mock()
    monkeypatch.setattr(local, "backup_redis_data", Mock())
    monkeypatch.setattr(local, "TerraformWrapper", Mock(return_value=terraform))

    assert local.destroy(k3d, str(data_path), skip_confirmation=True)
    local.backup_redis_data.assert_not_called()
    assert backup_path.read_bytes() == b"immutable-rdb"
    assert (data_path / local.REDIS_MIGRATION_STATE).exists()


def test_redis_backup_uses_posix_shell_and_portable_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    kubectl = Mock(spec=KubectlWrapper)
    kubectl.context.return_value = nullcontext()
    kubectl.get_secret.return_value = base64.b64encode(b"secret").decode()
    kubectl.cp.side_effect = lambda source, destination: Path(destination).write_bytes(
        b"rdb"
    )
    monkeypatch.setattr(
        local,
        "find_redis_master",
        lambda kubectl: ("redis-master-0", "redis-master", "StatefulSet"),
    )

    assert local.backup_redis_data(
        kubectl,
        str(tmp_path),
        marker_key="migration-key",
        marker_value="migration-value",
    )
    command = kubectl.exec.call_args.args[1]
    assert command[:3] == ["sh", "-c", local.REDIS_BACKUP_COMMAND]
    assert "bash" not in command
    assert kubectl.exec.call_args.kwargs == {
        "capture_output": False,
        "censor_command": True,
    }

    invocations = tmp_path / "redis-cli-invocations"
    redis_cli = tmp_path / "redis-cli"
    redis_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        f"with open({str(invocations)!r}, 'a') as output:\n"
        "    output.write(json.dumps([os.environ['REDISCLI_AUTH'], sys.argv[1:]]) + '\\n')\n"
    )
    redis_cli.chmod(0o755)
    subprocess.run(
        command,
        check=True,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
    )
    assert [json.loads(line) for line in invocations.read_text().splitlines()] == [
        ["secret", ["SET", "migration-key", "migration-value"]],
        ["secret", ["CONFIG", "SET", "appendonly", "no"]],
        ["secret", ["SAVE"]],
    ]


def test_docker_config_secret_is_reapplied_without_plaintext(
    monkeypatch: pytest.MonkeyPatch,
):
    manifests = []
    artifacts = Mock(spec=OSArtifacts)
    artifacts.kubectl = "kubectl"

    def capture_manifest(command: Any, **kwargs: Any) -> str:
        manifests.append(json.loads(Path(command[-1]).read_text()))
        return ""

    monkeypatch.setattr(wrappers, "execute_cmd", capture_manifest)
    docker_config = base64.b64encode(b'{"auths":{"private.example":{}}}').decode()
    KubectlWrapper(artifacts, "test").apply_docker_config_secret(
        "acrtoken", docker_config
    )
    assert manifests == [
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "acrtoken"},
            "type": "kubernetes.io/dockerconfigjson",
            "data": {".dockerconfigjson": docker_config},
        }
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
