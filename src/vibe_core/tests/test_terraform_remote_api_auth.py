# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from pathlib import Path


def test_remote_api_auth_kubernetes_contract():
    terraform = (
        Path(__file__).parents[1]
        / "vibe_core"
        / "terraform"
        / "services"
        / "restapi.tf"
    ).read_text()

    assert '"LoadBalancer"' not in terraform
    assert 'type = "ClusterIP"' in terraform
    assert "nginx.ingress.kubernetes.io/rewrite-target" not in terraform
    assert "nginx.ingress.kubernetes.io/use-regex" not in terraform
    assert (
        'dynamic "env" {\n'
        "            for_each = var.local_deployment ? [] : [1]\n"
        "            content {\n"
        '              name = "FARMVIBES_API_TOKEN"\n'
        "              value_from {\n"
        "                secret_key_ref {\n"
        '                  name = "farmvibes-api-auth"\n'
        '                  key  = "token"'
    ) in terraform

    assert 'ingress_class_name = var.local_deployment ? "traefik" : "nginx"' in terraform
    assert (
        'dynamic "tls" {\n'
        "      for_each = var.local_deployment ? [] : [1]\n"
        "      content {\n"
        "        hosts       = [var.public_ip_fqdn]\n"
        '        secret_name = "terravibes-rest-api-tls"'
    ) in terraform
    assert '"cert-manager.io/cluster-issuer"            = "letsencrypt"' in terraform


def test_remote_services_use_pinned_native_images():
    root = Path(__file__).parents[1] / "vibe_core"
    terraform = root / "terraform"
    wrappers = (root / "cli" / "wrappers.py").read_text()
    redis = (terraform / "aks" / "modules" / "kubernetes" / "redis.tf").read_text()
    rabbitmq = (
        terraform / "aks" / "modules" / "kubernetes" / "rabbitmq.tf"
    ).read_text()

    assert '"redis_image": REDIS_IMAGE' in wrappers
    assert '"rabbitmq_image": RABBITMQ_IMAGE' in wrappers
    assert 'resource "helm_release"' not in redis + rabbitmq
    assert "image             = var.redis_image" in redis
    assert "image             = var.rabbitmq_image" in rabbitmq
    assert 'name = "rabbitmq-data"' in rabbitmq

    providers = (
        terraform / "aks" / "modules" / "kubernetes" / "providers.tf"
    ).read_text()
    assert 'source  = "hashicorp/random"' in providers
    services_providers = (terraform / "services" / "providers.tf").read_text()
    assert 'provider "helm"' not in services_providers
    assert 'backend "kubernetes"' in services_providers

    ensure_services = wrappers.split("def ensure_services(", 1)[1].split(
        "def ensure_local_cluster(", 1
    )[0]
    assert "backend_config=backend_config" in ensure_services
