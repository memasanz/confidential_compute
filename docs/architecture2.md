# Architecture: attestation and Azure resources

Companion to [ARCHITECTURE.md](ARCHITECTURE.md). It explains what Microsoft Azure
Attestation (MAA) does in this system and lists the Azure resources involved.
Resource names describe roles, not a particular deployed environment.

## What MAA is and how it is used

Microsoft Azure Attestation (MAA) is an Azure service that verifies hardware
attestation evidence and returns a signed token only when the evidence is valid.

- Each worker runs in an AMD SEV-SNP confidential container. The hardware
  produces a signed report of what is running: the policy hash and the
  firmware and security versions.
- MAA checks that report against Microsoft's trust roots and issues a signed
  token if it is valid.
- The gateway verifies the MAA token before sending any session data to a worker.
  The token must match the approved policy and meet the configured minimum
  security versions.
- The attestation challenge also binds the worker's TLS key fingerprint and its
  session public key to the evidence. This binding is what makes the attested
  TLS design work: the gateway trusts a key only because the hardware vouched
  for it.
- The deployment uses a shared regional MAA instance as the token issuer.

## Azure resources

**Networking** (an existing virtual network):

- A Container Apps infrastructure subnet (`Microsoft.Network/virtualNetworks/subnets`),
  which hosts the gateway and controller.
- A worker subnet delegated to Azure Container Instances
  (`Microsoft.Network/virtualNetworks/subnets`, delegation `Microsoft.ContainerInstance/containerGroups`).
  Its NSG (`Microsoft.Network/networkSecurityGroups`) denies worker-to-worker,
  worker-to-control and worker-to-state traffic.
- A NAT gateway for restricted worker egress (`Microsoft.Network/natGateways`).
- Private DNS zones for the internal ingress and the worker hostnames
  (`Microsoft.Network/privateDnsZones`).
- VPN or ExpressRoute from on-premises (`Microsoft.Network/virtualNetworkGateways`
  or an ExpressRoute circuit, typically already in place). This supplies a route
  only; it does not authenticate callers.

**Control side:**

- A Container Apps environment, internal (`Microsoft.App/managedEnvironments`),
  with two apps, the gateway and the controller (`Microsoft.App/containerApps`).
- User-assigned managed identities for the gateway, the controller and the
  registry pull (`Microsoft.ManagedIdentity/userAssignedIdentities`).
- A private, Entra-only Azure Table storage account for the lease store
  (`Microsoft.Storage/storageAccounts`, Table service, with a private endpoint,
  `Microsoft.Network/privateEndpoints`).
- An Entra app registration for the API, with the `Sandbox.Access` app role
  (Microsoft Entra ID application, not an ARM resource).

**Workers:**

- Confidential ACI container groups (AMD SEV-SNP), created and deleted by the
  controller per session and never reused (`Microsoft.ContainerInstance/containerGroups`).
- The attestation sidecar, a second container in each worker group, used for
  evidence only. It is not a separate resource.

**Shared services:**

- Azure Container Registry for the worker, sidecar and controller images, with
  pull-only access through the pull identity (`Microsoft.ContainerRegistry/registries`).
- Microsoft Azure Attestation, using a regional endpoint (`Microsoft.Attestation/attestationProviders`
  for a private provider; a shared regional endpoint needs nothing to provision).
- Azure Policy, applied at the subscription level to constrain administrative
  access and what can be deployed (`Microsoft.Authorization/policyAssignments`).
  It is not deployed by this project.

**Role assignments** (`Microsoft.Authorization/roleAssignments`):

- The controller identity creates and deletes ACI groups and uses the table.
- The gateway identity uses the table.
- The pull identity has pull-only access to the registry.
