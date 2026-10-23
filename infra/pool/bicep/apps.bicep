// Pool gateway (internal ingress, Entra-validated MCP endpoint) and controller (no ingress) apps.
// Separate user-assigned identities. The attached pull identity has AcrPull only. Only the controller
// mounts the ephemeral material volume and receives the worker-server CA private key.
param location string = resourceGroup().location
param environmentName string = 'cae-confidential-pool'
param image string
param registryServer string
param pullIdentityId string
param gatewayIdentityId string
param gatewayClientId string
param controllerIdentityId string
param controllerClientId string
param gatewayName string = 'pool-gateway'
param controllerName string = 'pool-controller'
param gatewayUrl string
@description('Shared, non-secret service settings (POOL_* name/value).')
param common object
param gatewayEnv object
param controllerEnv object
@secure()
param serverCaCert string
@secure()
param serverCaKey string
@secure()
param clientCaCert string
@secure()
param gatewayClientCert string
@secure()
param gatewayClientKey string
param controllerMinReplicas int = 1
param controllerMaxReplicas int = 1

resource env 'Microsoft.App/managedEnvironments@2024-03-01' existing = { name: environmentName }

var registries = [{ server: registryServer, identity: pullIdentityId }]
var commonEnv = [for item in items(common): { name: item.key, value: string(item.value) }]
var gatewayExtra = [for item in items(gatewayEnv): { name: item.key, value: string(item.value) }]
var controllerExtra = [for item in items(controllerEnv): { name: item.key, value: string(item.value) }]
var pkiFiles = [
  { name: 'server-ca-cert', path: 'server-ca.pem' }
  { name: 'gateway-client-cert', path: 'gateway-client.pem' }
  { name: 'gateway-client-key', path: 'gateway-client.key' }
]

resource gateway 'Microsoft.App/containerApps@2024-03-01' = {
  name: gatewayName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${gatewayIdentityId}': {}, '${pullIdentityId}': {} }
  }
  properties: {
    managedEnvironmentId: env.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      registries: registries
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      secrets: [
        { name: 'server-ca-cert', value: serverCaCert }
        { name: 'gateway-client-cert', value: gatewayClientCert }
        { name: 'gateway-client-key', value: gatewayClientKey }
      ]
    }
    template: {
      containers: [{
        name: 'gateway'
        image: image
        args: ['gateway']
        resources: { cpu: json('0.5'), memory: '1Gi' }
        env: concat(commonEnv, gatewayExtra, [
          { name: 'POOL_IDENTITY_CLIENT_ID', value: gatewayClientId }
          { name: 'POOL_GATEWAY_URL', value: gatewayUrl }
          { name: 'POOL_SERVER_CA', value: '/pki/server-ca.pem' }
          { name: 'POOL_CLIENT_CERT', value: '/pki/gateway-client.pem' }
          { name: 'POOL_CLIENT_KEY', value: '/pki/gateway-client.key' }
        ])
        volumeMounts: [{ volumeName: 'pki', mountPath: '/pki' }]
      }]
      volumes: [{
        name: 'pki'
        storageType: 'Secret'
        secrets: [for f in pkiFiles: { secretRef: f.name, path: f.path }]
      }]
      scale: { minReplicas: 1, maxReplicas: 2 }
    }
  }
}

resource controller 'Microsoft.App/containerApps@2024-03-01' = {
  name: controllerName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${controllerIdentityId}': {}, '${pullIdentityId}': {} }
  }
  properties: {
    managedEnvironmentId: env.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      registries: registries
      secrets: [
        { name: 'server-ca-cert', value: serverCaCert }
        { name: 'server-ca-key', value: serverCaKey }
        { name: 'client-ca-cert', value: clientCaCert }
        { name: 'gateway-client-cert', value: gatewayClientCert }
        { name: 'gateway-client-key', value: gatewayClientKey }
      ]
    }
    template: {
      containers: [{
        name: 'controller'
        image: image
        args: ['controller']
        resources: { cpu: json('1.0'), memory: '2Gi' }
        env: concat(commonEnv, controllerExtra, [
          { name: 'POOL_IDENTITY_CLIENT_ID', value: controllerClientId }
          { name: 'POOL_SERVER_CA', value: '/pki/server-ca.pem' }
          { name: 'POOL_CLIENT_CERT', value: '/pki/gateway-client.pem' }
          { name: 'POOL_CLIENT_KEY', value: '/pki/gateway-client.key' }
          { name: 'POOL_CA_CERT', value: '/pki/server-ca.pem' }
          { name: 'POOL_CA_KEY', value: '/pki/server-ca.key' }
          { name: 'POOL_GATEWAY_CLIENT_CA', value: '/pki/client-ca.pem' }
          { name: 'POOL_MATERIAL_ROOT', value: '/app/pool-control/material' }
        ])
        volumeMounts: [
          { volumeName: 'pki', mountPath: '/pki' }
          { volumeName: 'material', mountPath: '/app/pool-control/material' }
        ]
      }]
      volumes: [
        {
          name: 'pki'
          storageType: 'Secret'
          secrets: [
            { secretRef: 'server-ca-cert', path: 'server-ca.pem' }
            { secretRef: 'server-ca-key', path: 'server-ca.key' }
            { secretRef: 'client-ca-cert', path: 'client-ca.pem' }
            { secretRef: 'gateway-client-cert', path: 'gateway-client.pem' }
            { secretRef: 'gateway-client-key', path: 'gateway-client.key' }
          ]
        }
        {
          name: 'material'
          storageType: 'EmptyDir'
        }
      ]
      scale: { minReplicas: controllerMinReplicas, maxReplicas: controllerMaxReplicas }
    }
  }
}

output gatewayFqdn string = gateway.properties.configuration.ingress.fqdn
