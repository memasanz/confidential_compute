// Private zone named for the internal Container Apps environment default domain. Apps are reached at
// <app>.<defaultDomain>; '@' and '*' resolve to the environment static IP. Linked to the VNet so VNet/VPN
// clients using the VNet DNS server (resolver inbound endpoint) resolve it. No resolver changes.
param defaultDomain string
param staticIp string
param vnetId string

resource zone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: defaultDomain
  location: 'global'
}

resource link 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: zone
  name: 'link-vnet-fabric-foundry'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: vnetId }
  }
}

resource apex 'Microsoft.Network/privateDnsZones/A@2024-06-01' = {
  parent: zone
  name: '@'
  properties: {
    ttl: 300
    aRecords: [{ ipv4Address: staticIp }]
  }
}

resource wildcard 'Microsoft.Network/privateDnsZones/A@2024-06-01' = {
  parent: zone
  name: '*'
  properties: {
    ttl: 300
    aRecords: [{ ipv4Address: staticIp }]
  }
}

output zoneId string = zone.id
