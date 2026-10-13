# Shared confidential pool infrastructure

Staged, idempotent deployment of the shared pool prerequisites. No gateway/controller
apps and no workers are deployed here. Generated files go to `infra\pool\out\` (git-ignored).
No secrets are stored; the Log Analytics key is read by ARM at deploy time.

```powershell
cd infra\pool
.\deploy-pool-infra.ps1 -Stage preflight
# each stage runs what-if, then validate, then deploy; add -WhatIfOnly to stop after what-if
.\deploy-pool-infra.ps1 -Stage network      # RGs, NSG, control + worker subnets
.\deploy-pool-infra.ps1 -Stage identity     # id-pool-gateway / -controller / -acrpull
.\deploy-pool-infra.ps1 -Stage state        # Entra-only Table + private endpoint + privatelink zone
.\deploy-pool-infra.ps1 -Stage dns          # workers.pool.internal
.\deploy-pool-infra.ps1 -Stage rbac         # custom roles + scoped assignments
.\deploy-pool-infra.ps1 -Stage environment  # internal Container Apps environment
```

## Layout

| Resource | Group / scope |
|---|---|
| `snet-pool-control` 192.168.5.0/27 (Microsoft.App/environments) | existing VNet |
| `snet-pool-workers` 192.168.6.0/24 (Microsoft.ContainerInstance/containerGroups), NSG `nsg-pool-workers`, existing `natgw-aci` | existing VNet |
| NSG, identities, storage/Table, private endpoint, DNS zones, environment | `rg-confidential-pool-control` |
| ACI worker groups only (controller can create/delete here) | `rg-confidential-pool` |

Existing subnets (including the now-unused `snet-aci`), ACR, VPN, and resolver are not modified. The pool uses no HSM or Secure Key Release; see `docs\isolated-session-pool.md`.
The only writes to existing resources are new role assignments (AcrPull on the registry, subnet join) and
the new subnets in the shared VNet; the NAT gateway gains the worker subnet association.

## Worker NSG

Inbound: control subnet -> TCP 8000 only; worker->worker denied; everything else denied.
Outbound: DNS to the resolver (192.168.1.36), TCP 443 to `AzureAttestation` and `AzureActiveDirectory`,
optional TCP 443 to `AzureContainerRegistry.EastUS2`/`Storage.EastUS2` (`-AllowImagePullEgress`, default on for
image pull; disable once pulls use a private path); then deny the whole VNet (control, state, PE subnets) and Internet.
Package/git egress is off. NAT is attached because MAA and ACR have no private path; it is not an allowlist.
Gateway and controller share the control subnet, so their separation is by identity/RBAC, not network.

## State, identities, RBAC

Table `poolstate` in `stpoolstate<sub6>`: shared keys disabled, public access disabled, TLS 1.2, private endpoint
in `snet-pe2`, `privatelink.table.core.windows.net` linked to the VNet. Use one partition (`POOL_PARTITION`, e.g. `pool`)
because leasing relies on same-partition transactions.

| Identity | Roles (only) |
|---|---|
| `id-pool-gateway` | Storage Table Data Contributor on the table |
| `id-pool-controller` | Storage Table Data Contributor on the table; custom *ACI Worker Manager* on `rg-confidential-pool`; custom *DNS A Record Writer* on `workers.pool.internal`; custom *Worker Subnet Join* on `snet-pool-workers`; Managed Identity Operator on `id-pool-acrpull` |
| `id-pool-acrpull` | AcrPull on `acrconfmcpvisdzn` only |

Custom role definitions are subscription-level objects whose assignable scopes are single resource groups.

## DNS and ingress (to finish when apps are deployed)

- Workers: controller writes A records in `workers.pool.internal` (`POOL_DNS_ZONE`, `POOL_DNS_RESOURCE_GROUP=rg-confidential-pool-control`).
  VPN/VNet clients use the VNet DNS server (dnspr inbound endpoint 192.168.1.36), which resolves linked private zones; no resolver change.
- Gateway ingress: the environment is internal. Create a private zone named for `defaultDomain` (see `out\environment.json`),
  link it to the VNet, add `*` and `@` A records to the environment `staticIp`, or a friendly name pointing to it.
- Gateway/controller apps must attach their user-assigned identities and set `POOL_IDENTITY_CLIENT_ID`.
