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
from typing import Callable, Optional, Sequence

from vibe_core.cli.constants import RABBITMQ_IMAGE, REDIS_IMAGE
from vibe_core.cli.local import (
    DEFAULT_HOST,
    DEFAULT_PORT,
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
        time.sleep(1)
    raise AssertionError(f"Timed out waiting for {description}")


def redis_is_failing(kubectl_context: Sequence[str]) -> bool:
    result = subprocess.run(
        list(kubectl_context) + ["get", "pod", "redis-master-0", "-o", "json"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        return False
    statuses = json.loads(result.stdout).get("status", {}).get("containerStatuses", [])
    return bool(
        statuses
        and statuses[0].get("restartCount", 0) > 0
        and not statuses[0].get("ready", False)
    )


def redis_rollout_process(root_pid: int) -> Optional[int]:
    processes = output(["ps", "-eo", "pid=,ppid=,args="]).splitlines()
    parents = {}
    commands = {}
    for process in processes:
        fields = process.strip().split(maxsplit=2)
        if len(fields) == 3:
            pid, parent, command = fields
            parents[int(pid)] = int(parent)
            commands[int(pid)] = command

    for pid, command in commands.items():
        if "rollout status statefulset/redis-master" not in command:
            continue
        ancestor = pid
        while ancestor in parents and ancestor != root_pid:
            ancestor = parents[ancestor]
        if ancestor == root_pid:
            return pid
    return None


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
            f"redis.image={REDIS_IMAGE}",
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

    farmvibes_ai = shutil.which("farmvibes-ai")
    assert farmvibes_ai is not None
    update_command = [
        farmvibes_ai,
        "local",
        "update",
        "--auto-confirm",
        "--cluster-name",
        cluster_name,
        "--storage-path",
        str(storage_path),
        "--worker-replicas",
        "1",
    ]
    backup_path = storage_path / "data" / "redis-dump.rdb"
    first_update = subprocess.Popen(update_command, text=True)
    try:
        def cluster_was_recreated() -> bool:
            created = server_created(k3d)
            return bool(created and created != old_server_created)

        wait_until(
            cluster_was_recreated,
            "the recreated native cluster",
            first_update,
        )
        wait_until(
            lambda: backup_path.exists() and backup_path.stat().st_size > 0,
            "the Redis backup",
            first_update,
        )
        original_backup = backup_path.read_bytes()
        backup_path.write_bytes(b"not a redis dump\n")

        wait_until(
            lambda: redis_is_failing(kubectl_context),
            "Redis to reject the corrupt dump",
            first_update,
        )
        assert first_update.poll() is None, (
            "Migration reported success while Redis was still rejecting dump.rdb"
        )
        rollout_pid: Optional[int] = None

        def find_rollout() -> bool:
            nonlocal rollout_pid
            rollout_pid = redis_rollout_process(first_update.pid)
            return rollout_pid is not None

        wait_until(
            find_rollout,
            "the Redis readiness rollout check",
            first_update,
            timeout_s=60,
        )
        assert rollout_pid is not None
        # A rejected RDB would make the real rollout wait for its full timeout.
        # Stop only that scoped check after proving the update remains blocked.
        os.kill(rollout_pid, signal.SIGTERM)
        assert first_update.wait(timeout=120) != 0
    finally:
        if first_update.poll() is None:
            first_update.terminate()
            first_update.wait(timeout=30)

    migration_state = storage_path / "data" / REDIS_MIGRATION_STATE
    assert migration_state.exists()
    assert k3d.get_cluster_config() == legacy_config

    backup_path.write_bytes(original_backup)
    run(update_command)
    assert not migration_state.exists()
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
    ) == REDIS_IMAGE
    assert resource_field(
        kubectl_context, "statefulset", "rabbitmq", IMAGE_PATH
    ) == RABBITMQ_IMAGE
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

    new_password = secret_password(kubectl_context, "redis", "redis-password")
    assert new_password != old_password
    restored_value = redis_command(kubectl_context, new_password, "GET", redis_key)
    assert restored_value == redis_value
    print(
        "Redis migration retry survived a rejected dump: "
        f"key={redis_key} value={restored_value} old_pvc={old_pvc_uid} new_pvc={new_pvc_uid}"
    )
