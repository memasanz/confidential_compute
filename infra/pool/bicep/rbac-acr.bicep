// AcrPull for the pull-only identity, scoped to one registry (module deploys into the registry's RG).
param registryName string
param principalId string
param acrPullRoleId string

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = { name: registryName }

resource ra 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, principalId, acrPullRoleId)
  scope: acr
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRoleId
  }
}
