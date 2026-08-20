# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import base64
import os
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

from vibe_core.cli.constants import RABBITMQ_IMAGE, REDIS_IMAGE
from vibe_core.cli.local import DEFAULT_HOST, DEFAULT_PORT, REGISTRY_PORT
from vibe_core.cli.osartifacts import InstallType, OSArtifacts
from vibe_core.cli.wrappers import K3dWrapper

CHART_PATH = Path(__file__).parent / "legacy_local_services"
MANAGED_BY_PATH = ".metadata.labels.app\\.kubernetes\\.io/managed-by"
IMAGE_PATH = ".spec.template.spec.containers[0].image"


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
        agents=0,
        storage_path=str(storage_path),
        registry_port=REGISTRY_PORT,
        farmvibes_port=DEFAULT_PORT,
        host=DEFAULT_HOST,
    )

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
    run(
        [
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
    )

    run(kubectl_context + ["rollout", "status", "statefulset/redis-master", "--timeout=5m"])
    run(kubectl_context + ["rollout", "status", "statefulset/rabbitmq", "--timeout=5m"])
    backup_path = storage_path / "data" / "redis-dump.rdb"
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
        "Redis migration value survived: "
        f"key={redis_key} value={restored_value} old_pvc={old_pvc_uid} new_pvc={new_pvc_uid}"
    )
