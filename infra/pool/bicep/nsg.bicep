// Worker-subnet NSG. Default-deny: only gateway/controller -> worker, DNS, MAA, and image pull.
param location string = resourceGroup().location
param name string = 'nsg-pool-workers'
param controlCidr string
param workerCidr string
param vnetCidr string
param dnsResolverIp string
param workerPort string = '8000'
@description('Allow 443 to ACR/Storage tags so ACI can pull the image from a public ACR. Remove once the registry is private.')
param allowImagePullEgress bool = false

var allowRules = [
  { name: 'allow-control-to-worker-in', priority: 100, direction: 'Inbound', access: 'Allow', protocol: 'Tcp', src: controlCidr, dst: workerCidr, port: workerPort }
  { name: 'deny-worker-to-worker-in', priority: 200, direction: 'Inbound', access: 'Deny', protocol: '*', src: workerCidr, dst: '*', port: '*' }
  { name: 'deny-all-in', priority: 4000, direction: 'Inbound', access: 'Deny', protocol: '*', src: '*', dst: '*', port: '*' }
  { name: 'allow-dns-out-udp', priority: 100, direction: 'Outbound', access: 'Allow', protocol: 'Udp', src: workerCidr, dst: dnsResolverIp, port: '53' }
  { name: 'allow-dns-out-tcp', priority: 101, direction: 'Outbound', access: 'Allow', protocol: 'Tcp', src: workerCidr, dst: dnsResolverIp, port: '53' }
  { name: 'allow-maa-out', priority: 110, direction: 'Outbound', access: 'Allow', protocol: 'Tcp', src: workerCidr, dst: 'AzureAttestation', port: '443' }
  { name: 'allow-aad-out', priority: 111, direction: 'Outbound', access: 'Allow', protocol: 'Tcp', src: workerCidr, dst: 'AzureActiveDirectory', port: '443' }
  { name: 'deny-vnet-out', priority: 4000, direction: 'Outbound', access: 'Deny', protocol: '*', src: '*', dst: vnetCidr, port: '*' }
  { name: 'deny-internet-out', priority: 4010, direction: 'Outbound', access: 'Deny', protocol: '*', src: '*', dst: 'Internet', port: '*' }
]

var pullRules = allowImagePullEgress ? [
  { name: 'allow-acr-out', priority: 120, direction: 'Outbound', access: 'Allow', protocol: 'Tcp', src: workerCidr, dst: 'AzureContainerRegistry.${location}', port: '443' }
  { name: 'allow-storage-pull-out', priority: 121, direction: 'Outbound', access: 'Allow', protocol: 'Tcp', src: workerCidr, dst: 'Storage.${location}', port: '443' }
] : []

resource nsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: name
  location: location
  properties: {
    securityRules: [for r in concat(allowRules, pullRules): {
      name: r.name
      properties: {
        priority: r.priority
        direction: r.direction
        access: r.access
        protocol: r.protocol
        sourcePortRange: '*'
        destinationPortRange: r.port
        sourceAddressPrefix: r.src
        destinationAddressPrefix: r.dst
      }
    }]
  }
}

output id string = nsg.id
