# Set up your own confidential execution service

This guide is for a team deploying this repository into **its own Azure
subscription**, even if the person following it is not an infrastructure
specialist.

**We assume your organization's shared Azure infrastructure already exists.**
These scripts add this application's resources to it; they do not build your
organization's network from scratch. You will need help from your Azure/network
and identity administrators. Do not guess network settings or security approvals.

If you only want to use a service that someone has already deployed, **do not
deploy it again**. Follow the [client setup guide](../../docs/getting-started.md)
instead.

## What you are setting up

Your on-premises agent will call one private MCP endpoint. Behind that endpoint:

- A **gateway** checks who is calling and forwards tools to the right session.
- A **controller** creates, checks and destroys workers.
- A **worker** is a dedicated confidential ACI container group where one
  analysis session runs. Its memory is protected by AMD SEV-SNP.
- A small **state store** records which worker belongs to which caller.

The gateway and controller run as ordinary Container Apps, **outside** the
confidential boundary. Foundry supplies model inference separately; this guide
does not deploy a Foundry project or model.

You will first prepare access, deploy the service's supporting resources,
approve its images/security settings, and finally run the sample notebooks.
Use synthetic or approved data only; sign-off for sensitive data is a separate
security decision.

## Before running anything: who needs to help?

| Person/team | What to ask them for |
|---|---|
| Azure subscription owner | The target subscription, deployment permission, provider/policy checks and confidential ACI quota in the chosen region |
| Network team | An existing private network, reserved address ranges, outbound connectivity and a working route/DNS path from on-premises |
| Identity administrator | The sandbox API app, an authorized caller app and `Sandbox.Access` permission |
| Security/service owner | Approved container images, attestation baseline, risk decisions and certificate/key ownership |
| You/application team | Run the approved commands, configure your client, and verify the synthetic examples |

**Contributor access alone may not be sufficient.** Deployment creates custom
roles and assigns permissions to application identities; your administrator
must provide the required role-definition/role-assignment permissions as well
as resource deployment rights. Entra app registration/admin consent is a
separate permission from Azure subscription access.

## Step 1: collect the existing infrastructure details

Send this table to your Azure/network team. Ask them to return the actual values
for **your target subscription**. The walkthrough assumes the shared resources
are in that subscription and the network resources are in the selected region;
it is not a cross-subscription networking recipe.

| What you need | Plain-language meaning | Where your administrator can find it |
|---|---|---|
| Subscription ID | The billing/administrative home for this deployment | Azure portal -> Subscriptions -> selected subscription |
| Region | Where the service runs; must support confidential ACI with sufficient quota | Administrator-approved deployment region |
| VNet resource group and name | The existing private network; a resource group is a management folder | Virtual networks -> selected VNet -> Overview |
| VNet address range | The network's IP address space, also called a CIDR | VNet -> Address space |
| Two available subnet ranges | Separate address ranges for control services and confidential workers | Network team must reserve non-overlapping ranges |
| Existing private-endpoint subnet name | Where private connections to storage will be placed | VNet -> Subnets |
| DNS resolver IP | A reachable DNS server that can resolve the service's private names | Network team's resolver/inbound-endpoint configuration |
| NAT gateway resource ID | The approved outbound path used by this walkthrough | NAT gateway -> Properties -> Resource ID |
| Container registry resource group and name | The existing ACR that stores the application's container images | Container registries -> selected registry -> Overview |
| Log Analytics workspace resource group and name | The existing destination for platform logs | Log Analytics workspaces -> selected workspace -> Overview |
| On-premises connectivity | VPN/ExpressRoute or another approved private route, plus DNS forwarding | Network team; this repo does not create VPN profiles |

**Do not choose the subnet ranges yourself.** They must fit inside the VNet,
not overlap other ranges, and meet the platform's subnet sizing/delegation
requirements. The network team should check those requirements. The script can
create the two new subnets, but it does **not repair existing subnets** with the
wrong range, delegation, firewall rules or NAT association.

For this walkthrough, have the team approve NAT and restricted registry-pull
egress. If your organization requires a different private/egress design, stop
and have the network owner adapt/review the deployment rather than copying the
settings below. NAT provides an outbound path; the firewall rules still limit
what workers can reach. General-purpose package downloads and Git access are
not enabled for workers.

**Checkpoint:** you have all the values above, an approved on-premises route,
and an administrator who has checked regional availability, quota and policy.

## Step 2: prepare your computer and choose the subscription

Use **PowerShell 7.4 or newer**, Python 3.11+, Azure CLI and Docker Desktop
running **Linux containers**. Docker is needed to generate the confidential
container policy, even though image builds below run in Azure.

Open the repository in your terminal. All commands in this guide run from the
**repository root**, the folder containing `README.md`, `infra` and `examples`.
Do not change into `infra\pool`.

```powershell
# Stop on PowerShell and native-command failures rather than continuing.
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

az login
az account list --query '[].{name:name,id:id}' -o table

# Replace this placeholder with the subscription your administrator approved.
$subscriptionId = '<SUBSCRIPTION_ID>'
az account set --subscription $subscriptionId
az account show --query '{name:name,id:id,tenant:tenantId}' -o table
```

Check the displayed subscription and tenant before continuing. A **tenant**
is the Entra directory that holds identities; a **subscription** holds Azure
resources. They are different IDs.

Then prepare local tools and Python dependencies. Skip virtual-environment
creation if you already have the repository's `.venv`.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r pool-control\requirements.txt -r mcp-build-sandbox\requirements.txt
az extension add --name containerapp
az extension add --name confcom
az bicep install
docker info
```

`docker info` must succeed against the Linux-container engine. On managed
devices, use your organization's approved Python package index; the
[setup guide](../../docs/getting-started.md#install-from-the-repository-root)
shows the Microsoft-managed-device proxy.

Keep using **the same PowerShell window**: the variables defined below are
only held in that window. If you close it, repeat the configuration blocks;
do not rerun completed deployments just to recreate variables.

## Step 3: fill in your deployment settings

Replace every `<PLACEHOLDER>` below with a value from your administrator.
These are deployment inputs, not credentials. Keep a private local copy of
your completed settings; do not publish your environment's details.

The `@{ ... }` block is a PowerShell settings table. Later, `@infraInputs`
passes that table to the script, so you do not need to type every setting again.
This is **separate from** the client `examples\.env` file used later.

```powershell
$infraInputs = @{
    Subscription         = $subscriptionId
    Location             = '<AZURE_REGION>'
    VnetRg               = '<EXISTING_VNET_RESOURCE_GROUP>'
    VnetName             = '<EXISTING_VNET_NAME>'
    VnetCidr             = '<VNET_ADDRESS_RANGE>'
    PeSubnetName         = '<EXISTING_PRIVATE_ENDPOINT_SUBNET_NAME>'
    ControlCidr          = '<RESERVED_CONTROL_SUBNET_RANGE>'
    WorkerCidr           = '<RESERVED_WORKER_SUBNET_RANGE>'
    DnsResolverIp        = '<REACHABLE_DNS_RESOLVER_IP>'
    NatGatewayId         = '<EXISTING_NAT_GATEWAY_RESOURCE_ID>'
    AcrRg                = '<EXISTING_REGISTRY_RESOURCE_GROUP>'
    AcrName              = '<EXISTING_REGISTRY_NAME>'
    LogWorkspaceName     = '<EXISTING_LOG_WORKSPACE_NAME>'
    LogWorkspaceRg       = '<EXISTING_LOG_WORKSPACE_RESOURCE_GROUP>'

    # Names for this application's new resources; agree these with your team.
    ControlRg            = 'rg-confidential-pool-control'
    WorkerRg             = 'rg-confidential-pool'
    ControlSubnetName    = 'snet-pool-control'
    WorkerSubnetName     = 'snet-pool-workers'
    WorkerZone           = 'workers.pool.internal'
    EnvName              = 'cae-confidential-pool'

    # Explicit network-team approval is required for these choices.
    AttachNat            = $true
    AllowImagePullEgress = $true
}

$operationalDirectory = '.azure\pool'
```

The scripts themselves default NAT attachment and image-pull egress to **off**.
This walkthrough opts in only for the network design approved in step 1.
Do not change the worker port from its runtime value of 8000.

`ControlRg` contains the service's management resources. `WorkerRg` contains
disposable confidential worker groups. Use dedicated names agreed with your
administrator; do not reuse an unrelated application's resource groups.

Local generated files go into:

| Folder | Contents / handling |
|---|---|
| `infra\pool\out` | Infrastructure outputs and generated role definitions |
| `.azure\pool` | Policy, certificates, private keys, deployment parameters and build context |

These folders are ignored by Git. **Ignored does not mean encrypted or safe
to share.** Protect them and do not attach their contents to public issues.

## Step 4: check, preview and deploy the supporting resources

First run the read-only preflight:

```powershell
.\infra\pool\deploy-pool-infra.ps1 -Stage preflight @infraInputs
```

It reports existing subnet overlaps, policies and confidential ACI quota.
**This is information to review, not a complete automatic pass/fail approval.**
Stop if there are overlaps, insufficient quota or policy restrictions, and ask
your administrator to resolve them.

For each stage, **preview it, review it, then deploy that same stage** before
moving to the next. Do not paste every command below as one unattended batch.

```powershell
# Preview the network additions. This does not deploy the stage.
.\infra\pool\deploy-pool-infra.ps1 -Stage network @infraInputs -WhatIfOnly

# Only after your administrator approves the proposed changes:
.\infra\pool\deploy-pool-infra.ps1 -Stage network @infraInputs
```

`-WhatIfOnly` means "show proposed changes", **not "run offline"**. It still
connects to Azure, selects the subscription and reads resource information.
Previews for resource groups that do not exist yet are limited. Missing earlier
resources can also prevent later previews.

Repeat that preview/deploy pair for these stages **in this order**, changing
`-Stage network` to the name in the first column:

| Stage | What it does | What you should see afterwards |
|---|---|---|
| `identity` | Creates service identities, so services do not need account passwords | Gateway, controller and registry-pull managed identities |
| `state` | Creates private storage for leases/session records | Storage account, `poolstate` Table and private endpoint |
| `dns` | Creates the workers' private name zone | `workers.pool.internal` linked to the VNet |
| `rbac` | Gives each identity only its required permissions | Role definitions/assignments; this needs the administrator's permission |
| `environment` | Creates the private Container Apps hosting environment | The environment, without gateway/controller apps yet |

For example, the next pair is:

```powershell
.\infra\pool\deploy-pool-infra.ps1 -Stage identity @infraInputs -WhatIfOnly
# Review, then:
.\infra\pool\deploy-pool-infra.ps1 -Stage identity @infraInputs
```

Stop on an error. Do not skip a failing stage or treat "resource already exists"
as proof that it has the right configuration. Without preview mode, deployment
performs validation and creation/update, with what-if where applicable.

**Checkpoint:** all six deployment stages have completed, and your
administrator can see the intended new resources. No workers or application
gateway have been deployed yet. Supporting Azure resources can already incur
charges; review the [cost assumptions](../../docs/session-pool-costs.md).

## Step 5: arrange API access with your identity administrator

Ask the identity administrator to follow
[Register and authorize the sandbox API](../../docs/getting-started.md#register-and-authorize-the-sandbox-api).
You need:

| Item | Why you need it |
|---|---|
| Entra tenant ID | Which directory the gateway should trust |
| Sandbox API application/client ID | Which API incoming tokens must be intended for |
| Enabled `Sandbox.Access` application role | The permission required to use the gateway |
| A separate authorized caller application | The on-premises application that will call MCP |
| Securely provisioned caller credentials | Used by the client, not stored in infrastructure source |

The API exposes `api://<API_APP_ID>`, uses version-2 access tokens, and the
caller needs the application permission plus admin consent. An ordinary
`az login` does not grant this API permission.

Keep these different IDs straight: **tenant ID**, **API app ID** and
**caller app ID** are not interchangeable. Application/client IDs also differ
from service-principal object IDs used for role assignments.

Do not distribute one application's secret as each user's personal login.
Sessions belong to the authenticated principal; callers sharing one app
identity share ownership. Foundry/model access is a separate identity setup.

## Step 6: build the images and prepare the worker trust settings

A **container image** packages the code that runs in Azure. A **digest** is its
exact content fingerprint, written as `sha256:...`; unlike a tag, it identifies
the specific image contents. The confidential policy pins approved digests.

This step involves builds, registry changes and security review. Have the
service owner approve the source and sidecar version before proceeding.
Builds can incur charges; they are not what-if previews.

### Build the worker and copy the approved attestation sidecar

```powershell
$registry = "$($infraInputs.AcrName).azurecr.io"
$workerTag = 'pilot-v1'
$sidecarTag = 'pilot-v1'

# Builds this repository's worker source and stores it in your registry.
az acr build --subscription $subscriptionId --registry $infraInputs.AcrName `
    --image "mcp-build-sandbox:$workerTag" .\mcp-build-sandbox

# The security/service owner must supply an approved source image digest.
# Do not substitute an unreviewed latest tag.
$sidecarSource = 'mcr.microsoft.com/aci/skr@sha256:<APPROVED_SOURCE_DIGEST>'
az acr import --subscription $subscriptionId --name $infraInputs.AcrName `
    --source $sidecarSource --image "aci/skr:$sidecarTag"

$workerDigest = az acr repository show --subscription $subscriptionId `
    --name $infraInputs.AcrName --image "mcp-build-sandbox:$workerTag" --query digest -o tsv
$sidecarDigest = az acr repository show --subscription $subscriptionId `
    --name $infraInputs.AcrName --image "aci/skr:$sidecarTag" --query digest -o tsv
```

Both resulting variables must contain `sha256:` followed by 64 lowercase hex
characters. If builds/imports are blocked by registry networking, permissions
or organization policy, ask the registry owner for an approved build/import
path. Do not enable public access just to get past the error.

### Generate service certificates

**PKI** here means the gateway's client certificate and the CA that signs it.
Workers do not receive a certificate: each generates its own TLS key inside the
TEE, and the gateway trusts it only after attestation binds that key.

```powershell
.\.venv\Scripts\python.exe infra\pool\make_pki.py --output "$operationalDirectory\pki"
```

The command refuses to overwrite existing files. For a new deployment, retain
these safely; for an existing deployment, **do not delete and regenerate them
casually**, because that changes service trust. Agree ownership, secure backup
and rotation with the service owner. Certificates expire (default 90 days); see
the rotation runbook in [operations](../../docs/operations.md#certificate-rotation).

The script has a legacy default path, so keep using the explicit output above.
`client-ca.key` is retained for rotation, not deployed into workers.

### Generate the approved confidential-container policy

**Attestation** is hardware-backed evidence that the worker meets your approved
configuration. The **CCE policy** describes the permitted worker containers and
startup configuration. You are generating that policy here, not testing an
actual confidential worker.

```powershell
az acr login --subscription $subscriptionId --name $infraInputs.AcrName

$pullIdentityId = "/subscriptions/$subscriptionId/resourceGroups/$($infraInputs.ControlRg)/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-pool-acrpull"

.\.venv\Scripts\python.exe infra\pool\make_policy_base.py `
    --worker-image "$registry/mcp-build-sandbox@$workerDigest" `
    --sidecar-image "$registry/aci/skr@$sidecarDigest" `
    --pull-identity $pullIdentityId --registry $registry `
    --output "$operationalDirectory\policy-base.json"
```

Docker and `az confcom` must work, and the workstation must be able to pull the
approved images. Have the generated policy reviewed before continuing.
Changing worker or sidecar source/images later requires rebuilding and
regenerating this approved policy.

**Checkpoint:** you have two image digests, protected certificate files and a
reviewed `.azure\pool\policy-base.json`.

## Step 7: configure private gateway DNS and build the control services

The gateway needs a name that resolves to its **private** address. This stage
creates a private DNS zone for the Container Apps environment and links it to
the VNet.

```powershell
$ingressInputs = @{
    Subscription = $subscriptionId
    ControlRg = $infraInputs.ControlRg
    VnetRg = $infraInputs.VnetRg
    VnetName = $infraInputs.VnetName
    EnvName = $infraInputs.EnvName
}

.\infra\pool\deploy-pool-apps.ps1 -Stage ingress-dns @ingressInputs -WhatIfOnly
# Review, then:
.\infra\pool\deploy-pool-apps.ps1 -Stage ingress-dns @ingressInputs
```

Ask the network team to confirm that on-premises DNS forwards private-name
queries to the approved reachable resolver. Creating an Azure DNS zone does
not automatically configure DNS on an on-premises workstation.

Next build the gateway/controller image, which includes the approved policy:

```powershell
$buildInputs = @{
    Subscription = $subscriptionId
    AcrName = $infraInputs.AcrName
    OperationalDirectory = $operationalDirectory
}
.\infra\pool\deploy-pool-apps.ps1 -Stage build @buildInputs
```

This is a **remote ACR build with no what-if mode**. The command prints a tag
and digest. Copy just the printed `sha256:...` digest into this variable:

```powershell
$controlDigest = '<PRINTED_CONTROL_IMAGE_DIGEST>'
```

Do not confuse the three digests: worker, attestation sidecar, and the
gateway/controller control image.

## Step 8: get security approval and deploy the gateway/controller

Before filling the settings below, ask your security/service owner for:

| Setting/decision | Meaning |
|---|---|
| Approved MAA issuer URL | The Microsoft Azure Attestation service whose signed evidence you trust; HTTPS origin, no extra path |
| Approved minimum SVN JSON | Minimum accepted security versions for worker firmware/platform components |
| SMT decision | Whether SMT-enabled hosts are acceptable for your threat model |
| Pull-identity approval | Worker code can reach its attached registry-pull identity; it must have AcrPull only on the intended registry |
| Controller-only material approval | Only authorized control-plane administrators/services can access the gateway client key and deployment material |

The minimum-version JSON must contain exactly six nonnegative-integer keys:
`bootloader`, `tee`, `snpfw`, `microcode`, `guest`, `uvm_guest`.
Have the owner supply the approved JSON in
`.azure\pool\approved-minimum-svn.json`. **Do not invent values, use all zeros,
or weaken checks just to make a deployment pass.**

SMT and identity/material settings are risk decisions, not harmless switches.
Approval strings in the script record the operator's assertion; they do **not**
automatically prove that RBAC, secrets or the platform meet the asserted policy.

```powershell
$minimumSvn = Get-Content "$operationalDirectory\approved-minimum-svn.json" -Raw

$appsInputs = @{
    Subscription = $subscriptionId
    ControlRg = $infraInputs.ControlRg
    WorkerRg = $infraInputs.WorkerRg
    VnetRg = $infraInputs.VnetRg
    VnetName = $infraInputs.VnetName
    AcrName = $infraInputs.AcrName
    EnvName = $infraInputs.EnvName
    Location = $infraInputs.Location
    WorkerSubnetName = $infraInputs.WorkerSubnetName
    WorkerCidr = $infraInputs.WorkerCidr
    WorkerZone = $infraInputs.WorkerZone
    ImageDigest = $controlDigest
    WorkerImageDigest = $workerDigest
    SidecarImageDigest = $sidecarDigest
    TenantId = '<ENTRA_TENANT_ID>'
    ApiAudience = '<SANDBOX_API_APPLICATION_ID>'
    ApiRole = 'Sandbox.Access'
    MaaIssuer = '<APPROVED_MAA_HTTPS_ORIGIN>'
    MinimumSvn = $minimumSvn

    # Use these assertions only after the actual access/risk review.
    AllowSmt = $false
    AllowPullRuntimeIdentity = $true
    PullRoleAttested = 'acr-pull-only'
    MaterialAclConfirmed = 'controller-only'

    PoolId = 'pool'
    ResourcePrefix = 'pool'
    DesiredReady = 0
    MaxTotal = 3
    QueueWait = 600
    OperationalDirectory = $operationalDirectory
}

.\infra\pool\deploy-pool-apps.ps1 -Stage apps @appsInputs -WhatIfOnly
# Review and approve, then:
.\infra\pool\deploy-pool-apps.ps1 -Stage apps @appsInputs
```

The command prints the private gateway MCP URL. Save it for client setup.
Apps preview runs ARM what-if **and ARM validate**, then skips creation.
Even preview generates `.azure\pool\apps.parameters.json` **containing private
keys**. Protect that file; do not paste it into chat, logs or public issues.

If hosts fail the SMT requirement, ask the security owner to review the evidence.
Do not set `AllowSmt` to true simply to bypass a failure.

### What the pool-size settings mean

| Setting above | Meaning |
|---|---|
| `DesiredReady = 0` | Do not proactively keep unused workers warm |
| `MaxTotal = 3` | At most three workers, including ready, provisioning, active and deleting workers |
| `QueueWait = 600` | A queued request can wait up to ten minutes |

Zero ready is **not a fully stopped or free service**: gateway/controller and
supporting resources still run, and session requests can create cold workers.

If you want to keep two workers warm, change
`$appsInputs.DesiredReady = 2`, then preview/deploy the apps stage again.
That means **two ready within the three-worker cap**, not three active plus two
ready. At three active sessions there is no warm reserve. Warm workers cost
money even when nobody is using them.

Do not casually change `PoolId`, `ResourcePrefix` or the PKI for an existing
deployment; the controller uses those values to recognize/manage its workers.

### Gateway replica count

The gateway holds each session's encryption key in process memory only, so the
template pins it to exactly one replica. Do not raise `maxReplicas`: a second
replica could not decrypt sessions established by the first, and a gateway
restart retires its active sessions (workers accept a session key only once).

### Scheduled orphan cleanup

`infra\pool\orphan_cleanup.py` deletes worker groups older than a maximum age
that the controller no longer tracks. Run it on a schedule (for example a
Container Apps job every 15 minutes with a managed identity limited to the worker
resource group) after first running it with `--dry-run`.

## Step 9: connect from on-premises and prove the service works

Provisioning succeeded does **not** mean the whole workflow has been tested.
From the actual machine that will run the agent:

1. Confirm VPN/private routing and DNS with the network team. The gateway name
   must resolve to the intended private ingress, and HTTPS must be reachable.
2. Have the operator publish the gateway certificate fingerprint over a trusted
   channel. Pin that certificate as shown in the
   [client setup guide](../../docs/getting-started.md#verify-and-pin-gateway-tls).
   Never disable TLS verification to bypass a certificate error.
3. Copy `examples\.env.example` to `examples\.env` and fill in your new
   environment's gateway, certificate path and API/caller credentials. The
   caller credentials belong in the trusted client, not a public notebook.
4. Install `examples\requirements-notebooks.txt` and run
   [notebook 1](../../examples/01_mcp_tools.ipynb) with the bundled synthetic
   spreadsheet. Confirm the downloaded result: total revenue **640**, units **64**.
5. Configure separate Foundry inference access and run
   [notebook 2](../../examples/02_agent_framework.ipynb), or the
   [on-premises agent script](../../examples/agent_framework_analysis.py).
6. Have the operator confirm the session was released and its actual Azure
   worker group was destroyed. A release response alone is not proof of deletion.

Complete the [live release gate](../../docs/operations.md#live-release-gate)
before declaring the deployment ready. Only synthetic/approved low-sensitivity
data should be used with this deployment. Summaries sent to Foundry leave the worker's
confidential boundary.

## If something fails

| Symptom | What to check / who can help |
|---|---|
| Script says an explicit input is missing | Replace the placeholder or restore the settings table in your current PowerShell window |
| Authorization/role-assignment error | Subscription administrator; resource creation and role assignment are separate permissions |
| Overlapping range, subnet or delegation error | Network team; do not pick a random replacement range |
| Registry build/import/pull failure | Registry owner; check permissions, image names, network path and build policy |
| `confcom` or Docker failure | Local Docker Linux engine, registry login, image access and approved policy inputs |
| No ready worker / queued or expired session | Controller health, cap, quota, failed attestation, provisioning and deletion backlog |
| Worker attestation fails | Security/service owner; compare issuer, approved policy, minimum versions and SMT decision |
| Gateway hostname cannot resolve or connect | Network team's on-premises routing, private DNS forwarding and reachable resolver |
| HTTP 401/403 | Identity team; check API audience/tenant, caller credentials, `Sandbox.Access` and admin consent |
| MCP works but agent inference fails | Foundry/model permission or configuration; it uses separate credentials |

Do not share keys/tokens/full deployment parameter files when asking for help.
Never replay an unknown-outcome code execution automatically.

## Operational notes for the service owner

The repo does not create a VPN, public gateway, Foundry project/model or a
production user-facing application. No HSM or Secure Key Release is required:
the `skr` sidecar is used only for attestation evidence.

The controller uses an ephemeral volume for policy and deployment material.
Worker TLS private keys are never held by the controller or gateway; they exist
only inside each worker's TEE.

To pause new warm provisioning, deploy the same reviewed apps configuration
with `DesiredReady = 0`. **This does not delete existing workers**. Stopping
spend requires an operator to inspect/release work, clean up the pool's groups
and separately decide which apps/shared resources to stop. Do not delete shared
networking/registry resources used by other workloads.

Offline tests use explicit fake Azure/model boundaries; they are not a live
deployment test. Production still needs load/failure tests, reliable on-premises
connectivity, monitoring, budgets, certificate/key rotation, a user-authorization
model and explicit security approval. See the
[design](../../docs/isolated-session-pool.md),
[architecture](../../docs/ARCHITECTURE.md) and
[costs](../../docs/session-pool-costs.md).
