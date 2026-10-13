// Role assignment at the subnet scope (module is deployed into the VNet's resource group).
param vnetName string
param subnetName string
param principalId string
param roleDefinitionId string

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' existing = {
  name: vnetName
}

resource subnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: vnet
  name: subnetName
}

resource ra 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(subnet.id, principalId, roleDefinitionId)
  scope: subnet
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: roleDefinitionId
  }
}
