param location string = resourceGroup().location
param gatewayName string = 'id-pool-gateway'
param controllerName string = 'id-pool-controller'
param acrPullName string = 'id-pool-acrpull'

resource gateway 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: gatewayName
  location: location
}

resource controller 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: controllerName
  location: location
}

resource acrPull 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: acrPullName
  location: location
}

output acrPullPrincipalId string = acrPull.properties.principalId
output acrPullClientId string = acrPull.properties.clientId
output acrPullId string = acrPull.id
output gatewayPrincipalId string = gateway.properties.principalId
output gatewayClientId string = gateway.properties.clientId
output gatewayId string = gateway.id
output controllerPrincipalId string = controller.properties.principalId
output controllerClientId string = controller.properties.clientId
output controllerId string = controller.id
