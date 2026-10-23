// Entra-only Table state behind a private endpoint. One table, one partition (see POOL_PARTITION):
// the store relies on same-partition entity-group transactions for atomic leasing.
param location string = resourceGroup().location
param storageName string
param tableName string = 'poolstate'
param vnetId string
param peSubnetId string

var zoneName = 'privatelink.table.${environment().suffixes.storage}'

resource sa 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_ZRS' }
  properties: {
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    publicNetworkAccess: 'Disabled'
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    networkAcls: { defaultAction: 'Deny', bypass: 'None' }
  }
}

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' = {
  parent: sa
  name: 'default'
}

resource table 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  parent: tableService
  name: tableName
}

resource zone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: zoneName
  location: 'global'
}

resource zoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: zone
  name: 'link-pool-vnet'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: vnetId }
  }
}

resource pe 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'pe-${storageName}-table'
  location: location
  properties: {
    subnet: { id: peSubnetId }
    privateLinkServiceConnections: [
      {
        name: 'table'
        properties: { privateLinkServiceId: sa.id, groupIds: ['table'] }
      }
    ]
  }
}

resource peDns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: pe
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      { name: 'table', properties: { privateDnsZoneId: zone.id } }
    ]
  }
}

output storageId string = sa.id
output tableId string = table.id
output tableEndpoint string = 'https://${storageName}.table.${environment().suffixes.storage}'
