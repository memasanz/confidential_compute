// Table-scoped and DNS-zone-scoped assignments in the control resource group.
param storageName string
param tableName string
param zoneName string
param gatewayPrincipalId string
param controllerPrincipalId string
param tableDataContributorRoleId string
param dnsWriterRoleId string
param acrPullName string = 'id-pool-acrpull'
param managedIdentityOperatorRoleId string

resource acrPullIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = { name: acrPullName }

// Lets the controller attach the pull-only identity to worker groups; scoped to that identity only.
resource controllerMio 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acrPullIdentity.id, controllerPrincipalId, managedIdentityOperatorRoleId)
  scope: acrPullIdentity
  properties: {
    principalId: controllerPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: managedIdentityOperatorRoleId
  }
}

resource sa 'Microsoft.Storage/storageAccounts@2023-05-01' existing = { name: storageName }
resource ts 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' existing = {
  parent: sa
  name: 'default'
}
resource table 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' existing = {
  parent: ts
  name: tableName
}
resource zone 'Microsoft.Network/privateDnsZones@2024-06-01' existing = { name: zoneName }

resource gatewayTable 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(table.id, gatewayPrincipalId, tableDataContributorRoleId)
  scope: table
  properties: {
    principalId: gatewayPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: tableDataContributorRoleId
  }
}

resource controllerTable 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(table.id, controllerPrincipalId, tableDataContributorRoleId)
  scope: table
  properties: {
    principalId: controllerPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: tableDataContributorRoleId
  }
}

resource controllerDns 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(zone.id, controllerPrincipalId, dnsWriterRoleId)
  scope: zone
  properties: {
    principalId: controllerPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: dnsWriterRoleId
  }
}
