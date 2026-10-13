// Controller-only material volume: a dedicated Azure Files share (per-worker TLS keys and bindings).
// Container Apps SMB mounts need an account key, so this account (unlike the Entra-only state account)
// must allow shared keys; it is isolated by: no public network access, private endpoint only, a share
// registered in the environment but declared as a volume ONLY by the controller app. The gateway app and
// identity have no mount and no role here. The key is read by ARM at deploy time and never output.
param location string = resourceGroup().location
param storageName string
param shareName string = 'poolmaterial'
param environmentName string = 'cae-confidential-pool'
param storageLinkName string = 'poolmaterial'
param vnetId string
param peSubnetId string

var zoneName = 'privatelink.file.${environment().suffixes.storage}'

resource sa 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_ZRS' }
  properties: {
    allowSharedKeyAccess: true
    publicNetworkAccess: 'Disabled'
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    networkAcls: { defaultAction: 'Deny', bypass: 'None' }
  }
}

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = {
  parent: sa
  name: 'default'
}

resource share 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileService
  name: shareName
  properties: { shareQuota: 5, enabledProtocols: 'SMB' }
}

resource zone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: zoneName
  location: 'global'
}

resource zoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: zone
  name: 'link-vnet-fabric-foundry'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: vnetId }
  }
}

resource pe 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'pe-${storageName}-file'
  location: location
  properties: {
    subnet: { id: peSubnetId }
    privateLinkServiceConnections: [
      { name: 'file', properties: { privateLinkServiceId: sa.id, groupIds: ['file'] } }
    ]
  }
}

resource peDns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: pe
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      { name: 'file', properties: { privateDnsZoneId: zone.id } }
    ]
  }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' existing = { name: environmentName }

resource envStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: env
  name: storageLinkName
  dependsOn: [peDns, share]
  properties: {
    azureFile: {
      accountName: storageName
      accountKey: sa.listKeys().keys[0].value
      shareName: shareName
      accessMode: 'ReadWrite'
    }
  }
}

output storageId string = sa.id
