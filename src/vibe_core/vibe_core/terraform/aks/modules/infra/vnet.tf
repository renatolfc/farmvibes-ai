data "http" "ip" {
  url = "https://ipv4.icanhazip.com"
}

resource "azurerm_network_security_group" "aks-nsg" {
  name                = "${var.prefix}-nsg"
  location            = var.location
  resource_group_name = var.resource_group_name

  security_rule {
    name                       = "allow_http"
    priority                   = 1001
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "80"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "allow_https"
    priority                   = 1101
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "443"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "allow_keyvault"
    priority                   = 1201
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "443"
    source_address_prefix      = trimspace(data.http.ip.response_body)
    destination_address_prefix = azurerm_private_endpoint.pe-kv.private_service_connection.0.private_ip_address
  }

  depends_on = [data.azurerm_resource_group.resourcegroup]
}

resource "azurerm_virtual_network" "vnet" {
  name                = "${var.prefix}-vnettf"
  location            = var.location
  resource_group_name = var.resource_group_name
  address_space       = ["10.224.0.0/12"]
  depends_on          = [data.azurerm_resource_group.resourcegroup]
}

resource "azurerm_subnet" "aks-subnet" {
  name                 = "aks"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.vnet.name
  address_prefixes     = ["10.224.0.0/16"]
  service_endpoints    = ["Microsoft.AzureCosmosDB", "Microsoft.KeyVault", "Microsoft.ServiceBus", "Microsoft.Storage"]
  depends_on           = [data.azurerm_resource_group.resourcegroup, azurerm_virtual_network.vnet]
}

resource "azurerm_subnet_network_security_group_association" "aks-subnet-nsg" {
  subnet_id                 = azurerm_subnet.aks-subnet.id
  network_security_group_id = azurerm_network_security_group.aks-nsg.id
  depends_on                = [azurerm_subnet.aks-subnet, azurerm_network_security_group.aks-nsg]
}

resource "azurerm_private_dns_zone" "keyvault" {
  name                = "privatelink.vaultcore.azure.net"
  resource_group_name = var.resource_group_name
  depends_on          = [data.azurerm_resource_group.resourcegroup]
}

resource "azurerm_private_endpoint" "pe-kv" {
  name                = "${var.prefix}-pe-kv"
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = azurerm_subnet.aks-subnet.id

  private_dns_zone_group {
    name    = "keyvaultprivatednszone"
    private_dns_zone_ids = [azurerm_private_dns_zone.keyvault.id]
  }

  private_service_connection {
    name                           = "${var.prefix}-pse-kv"
    private_connection_resource_id = azurerm_key_vault.keyvault.id
    is_manual_connection           = false
    subresource_names              = ["vault"]
  }
}
