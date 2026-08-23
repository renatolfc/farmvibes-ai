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
