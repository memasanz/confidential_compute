// Internal workload-profiles Container Apps environment (consumption only) in the control subnet.
// Gateway/controller apps are deliberately NOT deployed here.
param location string = resourceGroup().location
param name string = 'cae-confidential-pool'
param infraSubnetId string
param logWorkspaceName string
param logWorkspaceRg string

resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logWorkspaceName
  scope: resourceGroup(logWorkspaceRg)
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: name
  location: location
  properties: {
    vnetConfiguration: {
      infrastructureSubnetId: infraSubnetId
      internal: true
    }
    workloadProfiles: [
      { name: 'Consumption', workloadProfileType: 'Consumption' }
    ]
    zoneRedundant: false
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: law.properties.customerId
        sharedKey: law.listKeys().primarySharedKey
      }
    }
  }
}

output id string = env.id
output defaultDomain string = env.properties.defaultDomain
output staticIp string = env.properties.staticIp
