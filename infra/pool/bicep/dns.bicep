// Private zone for worker hostnames; the controller writes records here, VNet/VPN clients resolve it.
param zoneName string = 'workers.pool.internal'
param vnetId string

resource zone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: zoneName
  location: 'global'
}

resource link 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: zone
  name: 'link-pool-vnet'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: vnetId }
  }
}

output zoneId string = zone.id
