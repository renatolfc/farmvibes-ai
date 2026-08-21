# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import base64
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

from vibe_core.cli.constants import RABBITMQ_IMAGE
from vibe_core.cli.local import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    LOCAL_CONFIG,
    REDIS_MIGRATION_COMPLETE_STATE,
    REDIS_MIGRATION_STATE,
    REGISTRY_PORT,
)
from vibe_core.cli.osartifacts import InstallType, OSArtifacts
from vibe_core.cli.wrappers import K3dWrapper

CHART_PATH = Path(__file__).parent / "legacy_local_services"
MANAGED_BY_PATH = ".metadata.labels.app\\.kubernetes\\.io/managed-by"
IMAGE_PATH = ".spec.template.spec.containers[0].image"
MIGRATION_AGENTS = 1
MIGRATION_PORT = DEFAULT_PORT + 10
MIGRATION_WORKERS = 2
MIGRATION_REGISTRY = "mcr.microsoft.com/farmai"
MIGRATION_IMAGE_PREFIX = "terravibes/"
MIGRATION_IMAGE_TAG = "12072727496"
LEGACY_REDIS_IMAGE = "docker.io/library/redis:7.4.10-alpine"
MIGRATION_REDIS_IMAGE = "redis:7.4.10-bookworm"
MIGRATION_RABBITMQ_IMAGE = "rabbitmq:4.3.5-management"


def run(command: Sequence[str], capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        capture_output=capture_output,
        text=True,
    )


def output(command: Sequence[str]) -> str:
    return run(command, capture_output=True).stdout.strip()


def resource_field(
    kubectl_context: Sequence[str], kind: str, name: str, path: str
) -> str:
    return output(
        list(kubectl_context)
        + ["get", kind, name, "-o", f"jsonpath={{{path}}}"]
    )


def secret_password(
    kubectl_context: Sequence[str], name: str, key: str
) -> str:
    encoded = resource_field(kubectl_context, "secret", name, f".data.{key}")
    return base64.b64decode(encoded).decode()


def redis_command(
    kubectl_context: Sequence[str], password: str, command: str, *args: str
) -> str:
    result = subprocess.run(
        list(kubectl_context)
        + ["exec", "-i", "redis-master-0", "--", "redis-cli", "--no-auth-warning"],
        check=True,
        capture_output=True,
        input=f"AUTH {password}\n{' '.join((command,) + args)}\n",
        text=True,
    )
    return result.stdout.strip().splitlines()[-1]


def server_created(k3d: K3dWrapper) -> str:
    return next(
        (
            node["created"]
            for node in k3d.info().get("nodes", [])
            if node.get("role") == "server"
        ),
        "",
    )


def wait_until(
    predicate: Callable[[], bool],
    description: str,
    process: Optional[subprocess.Popen[str]] = None,
    timeout_s: int = 600,
):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        if process is not None and process.poll() is not None:
            raise AssertionError(
                f"Update exited with {process.returncode} before {description}"
            )
        time.sleep(0.1)
    raise AssertionError(f"Timed out waiting for {description}")


def read_migration_state(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def test_chart_to_native_redis_migration_preserves_data():
    cluster_name = os.environ["FARMVIBES_AI_CLUSTER_NAME"]
    storage_path = Path(os.environ["FARMVIBES_AI_STORAGE_PATH"])
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    commit = os.environ.get("GITHUB_SHA", "local")[:12]
    redis_key = f"farmvibes:migration:{run_id}:{run_attempt}"
    redis_value = f"survived:{commit}:{run_id}:{run_attempt}"

    artifacts = OSArtifacts()
    artifacts.check_dependencies(InstallType.LOCAL)
    k3d = K3dWrapper(artifacts, cluster_name)
    assert not k3d.cluster_exists(), f"Refusing to replace existing cluster {cluster_name}"

    storage_path.mkdir(parents=True, exist_ok=True)
    assert k3d.create(
        servers=1,
        agents=MIGRATION_AGENTS,
        storage_path=str(storage_path),
        registry_port=REGISTRY_PORT,
        farmvibes_port=MIGRATION_PORT,
        host=DEFAULT_HOST,
    )
    legacy_config = k3d.get_cluster_config()
    assert legacy_config == {
        "servers": 1,
        "agents": MIGRATION_AGENTS,
        "port": MIGRATION_PORT,
        "host": DEFAULT_HOST,
        "registry_port": REGISTRY_PORT,
    }
    old_server_created = server_created(k3d)
    assert old_server_created

    context = f"k3d-{cluster_name}"
    kubectl = artifacts.kubectl
    helm = artifacts.helm
    # The released Bitnami tags are gone; this chart keeps their Helm labels,
    # names, Secret keys, data path, and PVC names with maintained images.
    run(
        [
            helm,
            "upgrade",
            "--install",
            "legacy-local-services",
            str(CHART_PATH),
            "--kube-context",
            context,
            "--namespace",
            "default",
            "--wait",
            "--timeout",
            "5m",
            "--set-string",
            f"redis.image={LEGACY_REDIS_IMAGE}",
            "--set-string",
            f"rabbitmq.image={RABBITMQ_IMAGE}",
        ]
    )

    kubectl_context = [kubectl, "--context", context]
    run(kubectl_context + ["rollout", "status", "statefulset/redis-master", "--timeout=5m"])
    assert resource_field(
        kubectl_context, "statefulset", "redis-master", MANAGED_BY_PATH
    ) == "Helm"
    assert resource_field(
        kubectl_context, "statefulset", "rabbitmq", MANAGED_BY_PATH
    ) == "Helm"

    old_pvc_uid = resource_field(
        kubectl_context, "pvc", "redis-data-redis-master-0", ".metadata.uid"
    )
    old_password = secret_password(kubectl_context, "redis", "redis-password")
    assert redis_command(kubectl_context, old_password, "SET", redis_key, redis_value) == "OK"
    run(
        kubectl_context
        + [
            "create",
            "secret",
            "docker-registry",
            "acrtoken",
            "--docker-server=private.invalid",
            "--docker-username=robot",
            "--docker-password=test-token",
            "--docker-email=robot@invalid",
        ]
    )
    pull_secret = resource_field(
        kubectl_context, "secret", "acrtoken", ".data.\\.dockerconfigjson"
    )

    farmvibes_ai = shutil.which("farmvibes-ai")
    assert farmvibes_ai is not None
    update_without_config = [
        farmvibes_ai,
        "local",
        "update",
        "--auto-confirm",
        "--cluster-name",
        cluster_name,
    ]
    first_update_command = update_without_config + [
        "--storage-path",
        str(storage_path),
        "--registry",
        MIGRATION_REGISTRY,
        "--image-prefix",
        MIGRATION_IMAGE_PREFIX,
        "--image-tag",
        MIGRATION_IMAGE_TAG,
        "--worker-replicas",
        str(MIGRATION_WORKERS),
        "--log-level",
        "INFO",
        "--max-log-file-bytes",
        "123456",
        "--log-backup-count",
        "2",
        "--redis-image",
        MIGRATION_REDIS_IMAGE,
        "--rabbitmq-image",
        MIGRATION_RABBITMQ_IMAGE,
    ]
    destroy_command = [
        farmvibes_ai,
        "local",
        "destroy",
        "--auto-confirm",
        "--cluster-name",
        cluster_name,
    ]
    migration_state_path = storage_path / "data" / REDIS_MIGRATION_STATE
    first_update = subprocess.Popen(
        first_update_command,
        text=True,
        start_new_session=True,
    )
    try:
        wait_until(
            lambda: read_migration_state(migration_state_path).get("phase")
            == "cluster_created",
            "the post-k3d-create migration checkpoint",
            first_update,
        )
        os.killpg(first_update.pid, signal.SIGTERM)
        assert first_update.wait(timeout=120) != 0
    finally:
        if first_update.poll() is None:
            os.killpg(first_update.pid, signal.SIGTERM)
            first_update.wait(timeout=30)

    migration_state = read_migration_state(migration_state_path)
    assert migration_state["phase"] == "cluster_created"
    backup_path = storage_path / "data" / migration_state["backup_file"]
    original_backup = backup_path.read_bytes()
    original_checksum = migration_state["backup_sha256"]
    assert old_server_created != server_created(k3d)

    run(destroy_command)
    assert not k3d.cluster_exists()
    assert backup_path.read_bytes() == original_backup
    assert read_migration_state(migration_state_path)["backup_sha256"] == original_checksum

    backup_path.write_bytes(b"not the immutable redis dump\n")
    checksum_failure = subprocess.run(
        update_without_config,
        check=False,
        capture_output=True,
        text=True,
    )
    assert checksum_failure.returncode != 0
    assert "checksum mismatch" in (
        checksum_failure.stdout + checksum_failure.stderr
    ).lower()
    assert not k3d.cluster_exists()

    backup_path.write_bytes(original_backup)
    completed_path = storage_path / "data" / REDIS_MIGRATION_COMPLETE_STATE
    restore_update = subprocess.Popen(
        update_without_config,
        text=True,
        start_new_session=True,
    )
    try:
        wait_until(
            lambda: read_migration_state(migration_state_path).get("phase")
            == "restoring",
            "the native Redis restore phase",
            restore_update,
        )
        completed_path.mkdir()
        assert restore_update.wait(timeout=1200) != 0
    finally:
        if restore_update.poll() is None:
            os.killpg(restore_update.pid, signal.SIGTERM)
            restore_update.wait(timeout=30)

    restored_state = read_migration_state(migration_state_path)
    assert restored_state["phase"] == "restored"
    assert restored_state["backup_sha256"] == original_checksum
    assert k3d.get_cluster_config() == legacy_config
    completed_path.rmdir()

    run(kubectl_context + ["rollout", "status", "statefulset/redis-master", "--timeout=5m"])
    new_password = secret_password(kubectl_context, "redis", "redis-password")
    assert redis_command(kubectl_context, new_password, "GET", redis_key) == redis_value
    newer_key = f"{redis_key}:newer"
    newer_value = f"newer:{commit}:{run_id}:{run_attempt}"
    assert redis_command(kubectl_context, new_password, "SET", newer_key, newer_value) == "OK"

    run(update_without_config)
    assert not migration_state_path.exists()
    assert backup_path.read_bytes() == original_backup
    assert k3d.get_cluster_config() == legacy_config

    run(kubectl_context + ["rollout", "status", "statefulset/redis-master", "--timeout=5m"])
    run(kubectl_context + ["rollout", "status", "statefulset/rabbitmq", "--timeout=5m"])
    assert backup_path.stat().st_size > 0

    new_pvc_uid = resource_field(
        kubectl_context, "pvc", "redis-data-redis-master-0", ".metadata.uid"
    )
    assert new_pvc_uid != old_pvc_uid
    assert resource_field(
        kubectl_context, "statefulset", "redis-master", IMAGE_PATH
    ) == MIGRATION_REDIS_IMAGE
    assert resource_field(
        kubectl_context, "statefulset", "rabbitmq", IMAGE_PATH
    ) == MIGRATION_RABBITMQ_IMAGE
    assert resource_field(
        kubectl_context, "statefulset", "redis-master", MANAGED_BY_PATH
    ) != "Helm"
    assert (
        output(
            kubectl_context
            + ["get", "pod", "redisvolpod", "--ignore-not-found", "-o", "name"]
        )
        == ""
    )
    assert "legacy-local-services" not in output(
        [helm, "list", "--kube-context", context, "--namespace", "default", "--short"]
    )
    assert resource_field(
        kubectl_context, "deployment", "terravibes-worker", ".spec.replicas"
    ) == str(MIGRATION_WORKERS)
    assert resource_field(
        kubectl_context, "deployment", "terravibes-worker", IMAGE_PATH
    ) == (
        f"{MIGRATION_REGISTRY}/{MIGRATION_IMAGE_PREFIX}"
        f"worker:{MIGRATION_IMAGE_TAG}"
    )
    assert resource_field(
        kubectl_context, "secret", "acrtoken", ".data.\\.dockerconfigjson"
    ) == pull_secret

    assert new_password != old_password
    restored_value = redis_command(kubectl_context, new_password, "GET", redis_key)
    assert restored_value == redis_value
    assert redis_command(kubectl_context, new_password, "GET", newer_key) == newer_value
    saved_config = json.loads(
        (
            artifacts.config_dir
            / LOCAL_CONFIG.format(cluster_name=cluster_name)
        ).read_text()
    )
    assert saved_config == {
        "servers": 1,
        "agents": MIGRATION_AGENTS,
        "storage_path": str(storage_path),
        "registry": MIGRATION_REGISTRY,
        "log_level": "INFO",
        "max_log_file_bytes": 123456,
        "log_backup_count": 2,
        "image_tag": MIGRATION_IMAGE_TAG,
        "image_prefix": MIGRATION_IMAGE_PREFIX,
        "worker_replicas": MIGRATION_WORKERS,
        "enable_telemetry": False,
        "port": MIGRATION_PORT,
        "host": DEFAULT_HOST,
        "registry_port": REGISTRY_PORT,
        "redis_image": MIGRATION_REDIS_IMAGE,
        "rabbitmq_image": MIGRATION_RABBITMQ_IMAGE,
    }
    print(
        "Redis migration survived interruption, checksum rejection, and cleanup retry: "
        f"original={redis_key}:{restored_value} newer={newer_key}:{newer_value} "
        f"old_pvc={old_pvc_uid} new_pvc={new_pvc_uid}"
    )
