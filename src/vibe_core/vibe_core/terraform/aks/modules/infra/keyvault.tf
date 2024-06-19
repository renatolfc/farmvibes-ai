data "http" "ip" {
  url = "https://ipv4.icanhazip.com"
}

locals {
  network_acl = jsonencode({
    bypass                     = "AzureServices",
    default_action             = "Allow",
    virtual_network_subnet_ids = [azurerm_subnet.aks-subnet.id],
    ip_rules                   = [trimspace(data.http.ip.response_body)],
  })
  keyvault_name = "${var.prefix}-kv-${random_string.name_suffix.result}"
}

resource "null_resource" "keyvault" {
  provisioner "local-exec" {
    command = <<-EOT
              az keyvault create --name ${local.keyvault_name} --resource-group ${var.resource_group_name} --location ${var.location} --enabled-for-disk-encryption true --network-acls '${local.network_acl}' --sku standard || exit 1
              sleep 10
              az keyvault set-policy --name ${local.keyvault_name} --object-id ${data.azurerm_user_assigned_identity.kubernetesidentity.principal_id} --key-permissions get list --secret-permissions get list || exit 1
              sleep 10
              az keyvault set-policy --name ${local.keyvault_name} --object-id ${data.azurerm_client_config.current.object_id} --key-permissions create get --secret-permissions get backup delete list purge recover restore set || exit 1
              sleep 10
EOT
    when    = create
  }

  depends_on = [data.azurerm_resource_group.resourcegroup, data.http.ip, data.azurerm_user_assigned_identity.kubernetesidentity]
}

resource "null_resource" "cosmosdbsecret" {
  depends_on   = [null_resource.keyvault, azurerm_cosmosdb_sql_database.cosmosdb]

  provisioner "local-exec" {
    command = "az keyvault secret set --vault-name ${local.keyvault_name} --name cosmos-db-database --value ${azurerm_cosmosdb_sql_database.cosmosdb.name}"
    when    = create
  }
}

resource "null_resource" "cosmoscollectionsecret" {
  depends_on   = [null_resource.keyvault, azurerm_cosmosdb_sql_container.workflows]

  provisioner "local-exec" {
    command = "az keyvault secret set --vault-name ${local.keyvault_name} --name cosmos-db-collection --value ${azurerm_cosmosdb_sql_container.workflows.name}"
    when    = create
  }
}

resource "null_resource" "cosmosdbkey" {
  depends_on   = [null_resource.keyvault, azurerm_cosmosdb_account.cosmos]

  provisioner "local-exec" {
    command = "az keyvault secret set --vault-name ${local.keyvault_name} --name cosmos-db-key --value ${azurerm_cosmosdb_account.cosmos.primary_key}"
    when    = create
  }
}

resource "null_resource" "cosmosdburl" {
  depends_on   = [null_resource.keyvault, azurerm_cosmosdb_account.cosmos]

  provisioner "local-exec" {
    command = "az keyvault secret set --vault-name ${local.keyvault_name} --name cosmos-db-url --value ${azurerm_cosmosdb_account.cosmos.endpoint}"
    when    = create
  }
}

resource "null_resource" "storageconnectionstring" {
  depends_on   = [null_resource.keyvault, azurerm_storage_account.storageaccount]

  provisioner "local-exec" {
    command = "az keyvault secret set --vault-name ${local.keyvault_name} --name storage-account-connection-string --value ${azurerm_storage_account.storageaccount.primary_connection_string}"
    when    = create
  }
}

resource "null_resource" "staccosmosuri" {
  depends_on   = [null_resource.keyvault, azurerm_cosmosdb_account.staccosmos]

  provisioner "local-exec" {
    command = "az keyvault secret set --vault-name ${local.keyvault_name} --name stac-cosmos-db-url --value ${azurerm_cosmosdb_account.staccosmos.endpoint}"
    when    = create
  }
}

resource "null_resource" "staccosmoskeysecret" {
  depends_on   = [null_resource.keyvault, azurerm_cosmosdb_account.staccosmos]

  provisioner "local-exec" {
    command = "az keyvault secret set --vault-name ${local.keyvault_name} --name stac-cosmos-write-key --value ${azurerm_cosmosdb_account.staccosmos.primary_key}"
    when    = create
  }
}

resource "null_resource" "staccosmosdbname" {
  depends_on   = [null_resource.keyvault, azurerm_cosmosdb_sql_database.cosmosstacdb]

  provisioner "local-exec" {
    command = "az keyvault secret set --vault-name ${local.keyvault_name} --name stac-cosmos-db-name --value ${azurerm_cosmosdb_sql_database.cosmosstacdb.name}"
    when    = create
  }
}

resource "null_resource" "staccosmoscontainer" {
  depends_on   = [null_resource.keyvault, azurerm_cosmosdb_sql_container.staccontainer]

  provisioner "local-exec" {
    command = "az keyvault secret set --vault-name ${local.keyvault_name} --name stac-cosmos-container-name --value ${azurerm_cosmosdb_sql_container.staccontainer.name}"
    when    = create
  }
}

resource "null_resource" "staccosmosassetscontainer" {
  depends_on   = [null_resource.keyvault, azurerm_cosmosdb_sql_container.stacassetscontainer]

  provisioner "local-exec" {
    command = "az keyvault secret set --vault-name ${local.keyvault_name} --name stac-cosmos-assets-container-name --value ${azurerm_cosmosdb_sql_container.stacassetscontainer.name}"
    when    = create
  }
}
