# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import argparse
import base64
import binascii
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from vibe_core.cli import helper
from vibe_core.cli.constants import AZURE_CR_DOMAIN, MAX_WORKER_NODES, REMOTE_SERVICE_URL_PATH_FILE
from vibe_core.cli.helper import in_wsl, log_should_be_logged_in, verify_to_proceed
from vibe_core.cli.logging import ColorFormatter, log
from vibe_core.cli.osartifacts import OSArtifacts
from vibe_core.cli.wrappers import AzureCliWrapper, DaprWrapper, KubectlWrapper, TerraformWrapper
from vibe_core.security import (
    API_AUTH_SECRET_KEY as REMOTE_API_AUTH_TOKEN_KEY,
)
from vibe_core.security import (
    API_AUTH_SECRET_NAME as REMOTE_API_AUTH_SECRET,
)
from vibe_core.security import (
    REMOTE_API_TOKEN_FILENAME as REMOTE_API_TOKEN_FILE,
)

DESTROY_WARNING = (
    "Destroying the cluster will delete *ALL* resources under the resource group "
    "{resource_group}.\n\n"
    "This includes all the resources created by the farmvibes-ai script,\n"
    f"as well as {ColorFormatter.red}any other resources you might have created "
    f"{ColorFormatter.reset}in the resource group.\n\n"
    "This action cannot be undone.\n\n"
    "Do you wish to proceed? (Answering 'y' will wipe the resource group)"
)
REST_API_DEPLOYMENT = "terravibes-rest-api"


def _initialize_kubectl(
    az: AzureCliWrapper, terraform: TerraformWrapper
) -> Optional[KubectlWrapper]:
    config_context = terraform.get_kubernetes_config_context(az.cluster_name, az.resource_group)
    if not config_context:
        log("Couldn't get Kubernetes config context", level="error")
        return None
    return KubectlWrapper(
        az.os_artifacts, cluster_name=az.cluster_name, config_context=config_context
    )


def remote_api_token_from_secret(secret: Dict[str, Any]) -> str:
    data = secret.get("data")
    encoded = data.get(REMOTE_API_AUTH_TOKEN_KEY) if isinstance(data, dict) else None
    if not isinstance(encoded, str):
        raise ValueError("Remote API token Secret is missing its token")
    try:
        token = base64.b64decode(encoded, validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        raise ValueError("Remote API token Secret contains an invalid token") from None
    if not token:
        raise ValueError("Remote API token Secret contains an empty token")
    return token


def persist_remote_api_token(os_artifacts: OSArtifacts, token: str) -> None:
    path = Path(os_artifacts.private_config_dir) / REMOTE_API_TOKEN_FILE
    descriptor, temporary_path = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}."
    )
    try:
        os.chmod(temporary_path, 0o600)
        output = os.fdopen(descriptor, "w", encoding="utf-8")
        descriptor = -1
        with output:
            output.write(token)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if os.path.exists(temporary_path):
            os.remove(temporary_path)


def ensure_remote_api_token(
    os_artifacts: OSArtifacts,
    kubectl: KubectlWrapper,
) -> bool:
    existing_secret = kubectl.get_secret_or_none(REMOTE_API_AUTH_SECRET)
    if existing_secret is None:
        token = secrets.token_urlsafe(48)
        kubectl.upsert_opaque_secret(
            REMOTE_API_AUTH_SECRET, {REMOTE_API_AUTH_TOKEN_KEY: token}
        )
        changed = True
    else:
        token = remote_api_token_from_secret(existing_secret)
        changed = False
    persist_remote_api_token(os_artifacts, token)
    return changed


def prepare_remote_api_token(
    os_artifacts: OSArtifacts,
    kubectl: KubectlWrapper,
    rotate: bool,
) -> Optional[Tuple[str, str]]:
    """Provision/recover a token or prepare a recoverable rotation."""

    if not rotate:
        ensure_remote_api_token(os_artifacts, kubectl)
        return None
    existing_secret = kubectl.get_secret_or_none(REMOTE_API_AUTH_SECRET)
    if existing_secret is None:
        ensure_remote_api_token(os_artifacts, kubectl)
        return None
    old_token = remote_api_token_from_secret(existing_secret)
    persist_remote_api_token(os_artifacts, old_token)
    return old_token, secrets.token_urlsafe(48)


def activate_remote_api_token(
    os_artifacts: OSArtifacts,
    kubectl: KubectlWrapper,
    old_token: str,
    new_token: str,
) -> None:
    """Activate a rotated token, restoring the previous token on failure."""

    def deploy(token: str) -> None:
        kubectl.upsert_opaque_secret(
            REMOTE_API_AUTH_SECRET, {REMOTE_API_AUTH_TOKEN_KEY: token}
        )
        kubectl.restart("deployment", name=REST_API_DEPLOYMENT)
        kubectl.rollout_status("deployment", REST_API_DEPLOYMENT)
        persist_remote_api_token(os_artifacts, token)

    try:
        deploy(new_token)
    except Exception:
        try:
            deploy(old_token)
        except Exception as rollback_error:
            raise RuntimeError(
                "Remote API token rotation and rollback failed; recover the "
                "farmvibes-api-auth Secret before retrying"
            ) from rollback_error
        raise


def recover_remote_api_token(
    os_artifacts: OSArtifacts, kubectl: KubectlWrapper
) -> None:
    secret = kubectl.get_secret_or_none(REMOTE_API_AUTH_SECRET)
    if secret is None:
        raise ValueError("Remote API token Secret does not exist; run remote update")
    persist_remote_api_token(os_artifacts, remote_api_token_from_secret(secret))


def status(os_artifacts: OSArtifacts, az: AzureCliWrapper, environment: str) -> bool:
    # Detect if we're running in WSL
    if in_wsl() and az.is_file_in_mount():
        log(
            "Show URL command does not run correctly when run within WSL due AZ context issues\n"
            "Execute this script with the 'show_url' option on your Windows prompt to get the URL",
            level="error",
        )
        return False

    log("Refreshing AKS credentials...", level="debug")
    az.refresh_aks_credentials()
    terraform = TerraformWrapper(os_artifacts, az, environment=environment)
    kubectl = _initialize_kubectl(az, terraform)
    if not kubectl:
        return False
    try:
        recover_remote_api_token(os_artifacts, kubectl)
    except (OSError, ValueError):
        log(
            "Couldn't recover the remote API token. Run `farmvibes-ai remote update` "
            "with an authorized cluster account.",
            level="error",
        )
        return False
    log(f"Getting URL from ingress for cluster {az.cluster_name}...")
    url = kubectl.url_from_ingress(az.cluster_name)
    failed = False
    if not url:
        failed = True
        url = terraform.get_url_from_terraform_output(az.cluster_name, az.resource_group)

    if not url:
        log("Couldn't get URL for your AKS Cluster", level="error")
        return False

    url = url.replace('"', "")
    with open(os_artifacts.config_file(REMOTE_SERVICE_URL_PATH_FILE), "w") as f:
        f.write(url)

    log(f"URL for your AKS Cluster is: {url}")
    if failed:
        log(
            "We failed to get the URL from the cluster. "
            "The URL above might be incorrect, as we might have read it from old Terraform state. "
            "Please check the URL above and if it's incorrect, please run "
            "`farmvibes-ai remote update`.",
            level="warning",
        )
    return True


def check_cluster_name_length(cluster_name: str) -> bool:
    if len(cluster_name) > 15:
        log(
            "Cluster name is too long. Please use a shorter name (max 15 characters)",
            level="error",
        )
        return False
    return True


def setup_or_upgrade(
    os_artifacts: OSArtifacts,
    az: AzureCliWrapper,
    region: str,
    certificate_email: str,
    registry_path: str,
    registry_username: str,
    registry_password: str,
    image_prefix: str,
    image_tag: str,
    log_level: str,
    is_update: bool,
    max_worker_nodes: int = MAX_WORKER_NODES,
    enable_telemetry: bool = False,
    worker_replicas: int = 0,
    environment: str = "",
    current_user_name: str = "",
    rotate_api_token: bool = False,
) -> bool:
    assert environment, "Cloud environment name must be provided"
    if not worker_replicas:
        log(
            "No worker replicas specified. "
            "You can change this by re-running with "
            "`farmvibes-ai local setup --worker-replicas <number> ...`",
        )
        return False

    log(
        f"Trying to {'update' if is_update else 'create'} cluster in "
        f"region {region} and {environment} cloud environment..."
    )
    az.refresh_az_creds()
    try:
        subscription_id, tenant_id = az.get_subscription_and_tenant_id()
    except Exception as e:
        log_should_be_logged_in(e)
        return False

    if not az.check_resource_providers(region):
        return False

    terraform = TerraformWrapper(os_artifacts, az, environment=environment)
    try:
        workers, default = terraform.get_current_core_count() if is_update else (0, 0)
        az.verify_enough_cores_available(region, max_worker_nodes, workers, default)
    except Exception as e:
        log(
            f"Looks like you don't have enough cores available in your subscription. {e}",
            level="error",
        )
        return False

    if not check_cluster_name_length(az.cluster_name):
        return False

    log("Getting current user name...")
    current_user_name = current_user_name or az.get_current_user_name()

    log(f"Current user name is: {current_user_name}", level="debug")
    log("Verifying cluster already exists...")
    if az.cluster_exists() and not is_update:
        log(
            "Seems like you might have a cluster already created.",
            level="warning",
        )
        confirmation = verify_to_proceed("Do you want to delete your current cluster?")
        if confirmation:
            destroy(os_artifacts, az)
        else:
            log("Canceling installation...")
            raise Exception("Previous cluster exists. Cancelled.")

    log(
        f"Will {'update' if is_update else 'create'} cluster {az.cluster_name} "
        f"in resource group {az.resource_group}..."
    )
    created_rg = False
    try:
        if not is_update:
            created_rg = terraform.ensure_resource_group(
                tenant_id,
                subscription_id,
                region,
                az.cluster_name,
                az.resource_group,
            )
        else:
            az.refresh_aks_credentials()

        storage_name, container_name, storage_access_key = az.ensure_azurerm_backend(
            region,
        )
        if not storage_name or not container_name or not storage_access_key:
            log(
                "Couldn't create storage account for Terraform backend. "
                "Refusing to create cluster.",
                level="error",
            )
            return False

        if registry_path and registry_path.endswith(AZURE_CR_DOMAIN):
            if not registry_username or not registry_password:

                try:
                    registry_username = "00000000-0000-0000-0000-000000000000"
                    registry_password = az.request_registry_token(registry_path)
                except Exception:
                    log(
                        f"Couldn't infer registry credentials for {registry_path}. "
                        "Please provide them explicitly.",
                        level="error",
                    )
                    raise

        with terraform.workspace(f"farmvibes-aks-{az.cluster_name}-{az.resource_group}"):
            infra_results = terraform.ensure_infra(
                tenant_id,
                subscription_id,
                region,
                az.cluster_name,
                az.resource_group,
                max_worker_nodes,
                storage_name,
                container_name,
                storage_access_key,
                enable_telemetry,  # Required to create azure monitor and application insights
                cleanup_state=True,
                is_update=is_update,
            )

            dapr_updated = False
            kubectl = _initialize_kubectl(az, terraform)
            if not kubectl:
                log("Couldn't initialize kubectl, not updating", level="error")
                return False
            dapr = DaprWrapper(kubectl.os_artifacts, kubectl)
            if is_update and dapr.needs_upgrade():
                log("Upgrading Dapr CRDs")
                if not dapr.upgrade_crds():
                    log("Unable to upgrade Dapr CRDs", level="error")
                    return False
                dapr_updated = True

            k8s_results = terraform.ensure_k8s_cluster(
                az.cluster_name,
                tenant_id,
                registry_path,
                registry_username,
                registry_password,
                az.resource_group,
                current_user_name,
                certificate_email,
                infra_results["kubernetes_config_context"]["value"],
                infra_results["public_ip_address"]["value"],
                infra_results["public_ip_fqdn"]["value"],
                infra_results["public_ip_dns"]["value"],
                infra_results["keyvault_name"]["value"],
                infra_results["application_id"]["value"],
                infra_results["storage_connection_key"]["value"],
                infra_results["storage_account_name"]["value"],
                infra_results["userfile_container_name"]["value"],
                infra_results["monitor_instrumentation_key"]["value"],
                storage_name,
                container_name,
                storage_access_key,
                enable_telemetry,
                cleanup_state=True,
            )
            pending_rotation = prepare_remote_api_token(
                os_artifacts, kubectl, rotate_api_token
            )
            terraform.ensure_services(
                az.cluster_name,
                az.resource_group,
                registry_path,
                os_artifacts.config_file("kubeconfig"),
                infra_results["kubernetes_config_context"]["value"],
                infra_results["worker_node_pool_name"]["value"],
                infra_results["public_ip_fqdn"]["value"],
                image_prefix,
                image_tag,
                k8s_results["shared_resource_pv_claim_name"]["value"],
                k8s_results["otel_service_name"]["value"] if enable_telemetry else "",
                worker_replicas,
                log_level,
                cleanup_state=True,
            )

            with kubectl.context(kubectl.cluster_name):
                if pending_rotation is not None:
                    activate_remote_api_token(
                        os_artifacts, kubectl, *pending_rotation
                    )
                if dapr_updated:
                    log("dapr upgraded, restarting services")
                    kubectl.restart("deployment", selectors=["backend=terravibes"])
                    kubectl.rollout_status("deployment", REST_API_DEPLOYMENT)
                elif is_update and pending_rotation is None:
                    kubectl.restart("deployment", name=REST_API_DEPLOYMENT)
                    kubectl.rollout_status("deployment", REST_API_DEPLOYMENT)

    except Exception as e:
        log(f"{e.__class__.__name__}: {e}")
        log(
            f"Failed to {'update' if is_update else 'create'} cluster."
            f"{' Cleaning up...' if not is_update else ''}"
        )
        if is_update:
            log(
                "Skipping cluster deletion since this is an update, "
                "please try again later if the cluster is misbehaving."
            )
        else:
            confirmation = verify_to_proceed(
                "Do you wish the keep the cluster (Answering 'y' will leave the cluster as is)?"
            )
            if not confirmation:
                destroy(os_artifacts, az, created_rg)
            else:
                log(
                    "User opted to keep the cluster. Leaving it as is. "
                    "The cluster can be destroyed later by running the `destroy` subcommand."
                )
        return False

    return status(os_artifacts, az, environment)


def add_onnx(os_artifacts: OSArtifacts, az: AzureCliWrapper, file_to_upload: str, environment: str):
    if not az.cluster_exists():
        log("Cluster does not exist. Please create it first.", level="error")
        return False

    log("Refreshing AKS credentials...")
    az.refresh_az_creds()

    terraform = TerraformWrapper(os_artifacts, az, environment=environment)
    storage_account = terraform.get_storage_account_name()

    log("Getting storage connection string...")
    connection_string = az.get_storage_account_connection_string(storage_account)
    if not connection_string:
        log("Couldn't get storage connection string", level="error")
        return False

    log("Uploading files...")
    destination = os.path.join("onnx_resources", os.path.basename(file_to_upload))
    az.upload_file(file_to_upload, connection_string, destination)

    return True


def destroy(
    os_artifacts: OSArtifacts, az: AzureCliWrapper, destroy_rg: bool = False, confirm: bool = False
):
    log("Destroying cluster...")

    log("Verifying if group still exists...")
    if az.resource_group_exists():
        if confirm:
            confirmation = verify_to_proceed(
                DESTROY_WARNING.format(resource_group=az.resource_group)
            )
            if not confirmation:
                log("User opted to keep the cluster. Leaving it as is.")
                return False
        log("Group exists. Requesting destruction (this may take some time)...")
        resources = az.list_resources()
        az.delete_resources([r["id"] for r in resources])

        if destroy_rg:
            log("Destroying resource group, as it was created by us...")
            az.delete_resource_group()
    else:
        log("Group does not exist. Skipping destruction...")
        return False

    kubeconfig_file = os_artifacts.config_file("kubeconfig")
    if os.path.isfile(kubeconfig_file):
        os.remove(kubeconfig_file)

    terraform_directory = os_artifacts.terraform_directory
    for file in os.listdir(terraform_directory):
        os.remove(os.path.join(terraform_directory, file))

    log("Cluster destroyed.")
    return True


def add_secret(az: AzureCliWrapper, secret_name: str, secret_value: str, environment: str):
    kubectl = _initialize_kubectl(
        az, TerraformWrapper(az.os_artifacts, az, environment=environment)
    )
    if not kubectl:
        return False
    return kubectl.add_secret(secret_name, secret_value)


def delete_secret(az: AzureCliWrapper, secret_name: str, environment: str):
    kubectl = _initialize_kubectl(
        az, TerraformWrapper(az.os_artifacts, az, environment=environment)
    )
    if not kubectl:
        return False
    return kubectl.delete_secret(secret_name)


def restart(az: AzureCliWrapper, environment: str):
    kubectl = _initialize_kubectl(
        az, TerraformWrapper(az.os_artifacts, az, environment=environment)
    )
    if not kubectl:
        return False
    try:
        return kubectl.restart(
            "deployment", selectors=["backend=terravibes"], cluster_name=az.cluster_name
        )
    except Exception as e:
        log(f"Restart failed: {e}", level="error")
        return False


def dispatch(args: argparse.Namespace):
    os_artifacts = OSArtifacts()
    os_artifacts.check_dependencies()
    az = AzureCliWrapper(
        os_artifacts,
        args.cluster_name if hasattr(args, "cluster_name") else "",
        args.resource_group if hasattr(args, "resource_group") else "",
    )
    helper.AUTO_CONFIRMATION = args.auto_confirm

    # The below is needed for terraform/kubectl to find kubelogin
    original_path = os.environ["PATH"]
    os.environ["PATH"] += f"{os.pathsep}{os_artifacts.config_dir}"
    os.environ["ARM_ENVIRONMENT"] = args.environment

    ret: bool = False
    if args.action in {"setup", "update"}:
        az.refresh_az_creds()
        az.expand_azure_region(args.region.strip())
        enable_telemetry = args.enable_telemetry if hasattr(args, "enable_telemetry") else False
        ret = setup_or_upgrade(
            os_artifacts,
            az,
            args.region,
            args.cert_email,
            args.registry,
            args.registry_username,
            args.registry_password,
            args.image_prefix,
            args.image_tag,
            args.log_level,
            any([args.action in e for e in {"up", "upgrade", "update"}]),
            max_worker_nodes=args.max_worker_nodes,
            enable_telemetry=enable_telemetry,
            worker_replicas=args.worker_replicas,
            environment=args.environment,
            current_user_name=args.cluster_admin_name,
            rotate_api_token=getattr(args, "rotate_api_token", False),
        )
    elif args.action in {"destroy", "rm", "del", "remove"}:
        ret = destroy(os_artifacts, az, args.resource_group, confirm=True)
    elif args.action in {"show-url", "url", "status"}:
        ret = status(os_artifacts, az, args.environment)
    elif args.action in {"add-onnx", "add_onnx"}:
        ret = add_onnx(os_artifacts, az, args.model_path, args.environment)
    elif args.action == "add-secret":
        ret = add_secret(az, args.secret_name, args.secret_value, args.environment)
    elif args.action == "delete-secret":
        ret = delete_secret(az, args.secret_name, args.environment)
    elif args.action == "restart":
        ret = restart(az, args.environment)
    else:
        log(
            f"The command '{args.action}' is not supported. "
            "For more advanced cluster management, please use the Azure CLI directly "
            "or use the Azure Portal.",
            level="error",
        )
        if args.action in {"stop", "start"}:
            log(
                "Please see the documentation at "
                "https://learn.microsoft.com/en-us/azure/aks/start-stop-cluster "
                "for more information on how to stop or start your cluster.",
            )
        return False

    if args.action != "destroy":
        kubelogin_in_path = os.path.dirname(os_artifacts.kubelogin) in original_path
        if str(os_artifacts.config_dir) not in original_path and not kubelogin_in_path:
            # Warn user kubectl won't work without config_dir in path
            log(
                f"{os_artifacts.config_dir} not in PATH. "
                "Interacting with the cluster via kubectl will not work. "
                f"Please add {os_artifacts.config_dir} to your PATH.",
                level="warning",
            )

    return ret
