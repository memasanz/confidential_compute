# Getting started: pooled confidential sandbox

This guide describes deploying and onboarding the pooled design: one confidential
ACI group per session, an Entra-authenticated gateway, a Table lease store, and a
pool controller. It is a guided setup, not a fully automated installer. Nothing
is created in Azure until you run the deployment scripts.

To *use* an already-deployed pilot, go straight to the [pilot guide](pilot-guide.md).
Design and trust boundaries: [isolated-session-pool.md](isolated-session-pool.md).

## What belongs to the service, the application, and the user?

| Item | Who needs it? | Purpose |
|---|---|---|
| Gateway TLS certificate | The environment (self-signed, pinned by clients) | Authenticate and encrypt client-to-gateway traffic |
| Pool PKI (worker-server CA, gateway-client CA, gateway client cert) | Controller holds the worker CA; gateway holds its client key | Gateway-to-worker mutual TLS |
| Sandbox API app registration | One per environment | Defines the token audience and the `Sandbox.Access` app role |
| Client/agent app registration (service principal) | One per trusted calling application | Identifies the caller to the gateway |
| Client secret or certificate | The calling application, not each human user | Lets the application obtain an Entra token |
| Entra user identity | Each human user | Signs in to the front-end and, if configured, the VPN |
| VPN client profile | Each workstation using point-to-site VPN | Provides a route into the VNet |
| `id-pool-gateway`, `id-pool-controller`, `id-pool-acrpull` | The gateway, controller, and worker groups | Table access, worker management, image pull (see [infra\pool\README.md](../infra/pool/README.md)) |

Do **not** share a daemon's client secret with every user; human users use their
directory accounts. The gateway certificate, worker certificates, and VPN
certificate are different credentials, and the MAA attestation token is none of
them.

## 1. Prepare the repository and administrative access

Install Python 3.11+, Azure CLI with the `confcom` extension, and Docker in Linux-container
mode (needed only to generate the CCE policy). Create the virtual environment and
install `pool-control\requirements.txt`, `mcp-build-sandbox\requirements.txt` and `pytest`, then run
the local tests:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_pool_feasibility.py tests\test_pool_control.py
```

Copy `examples\.env.example` to `examples\.env` (git-ignored). Sign in with `az login`
and confirm the subscription before provisioning.

| Responsibility | Required access |
|---|---|
| Deploy ACI, Container Apps, storage, and network resources | Azure management-plane permissions on the target resource groups |
| Register apps and grant API permission | Entra app-registration and admin-consent permissions |
| Assign custom roles and role assignments | Role-assignment permission on the scopes in the infra README |
| DNS and VPN administration | Your DNS and network administrators |

## 2. Shared prerequisites

The pool deploys **into** an existing VNet; it does not create the VNet, VPN,
resolver, NAT gateway, or registry. Provide:

1. A VNet with a point-to-site or other route from callers, and a DNS server that
   resolves linked private zones (an Azure DNS Private Resolver works for VPN clients).
2. A NAT gateway (here `natgw-aci` / `pip-aci-nat`) that the worker subnet is attached to. MAA and ACR have
   no private path, so workers egress through it. NAT is not an egress allowlist.
3. An ACR (here `acrconfmcpvisdzn`) holding the worker image, digest-pinned.
4. A region supporting confidential ACI and enough confidential-core quota (20 workers = 60 cores).

The infra stages create the control and worker subnets (`snet-pool-control`,
`snet-pool-workers`), NSG, identities, private Table, DNS and the internal
Container Apps environment.

## 3. Register the sandbox API and the calling application

### Sandbox API registration

In **Microsoft Entra ID > App registrations**:

1. Register a single-tenant application for the sandbox API and record its
   **Application (client) ID** as `API_APP_ID`.
2. Under **Expose an API**, set the Application ID URI to `api://<API_APP_ID>`.
3. Under **App roles**, create an enabled role with value `Sandbox.Access`; allowed
   member type **Applications** (and **Users/Groups** if people sign in directly).
4. Set `api.requestedAccessTokenVersion` to `2` in the manifest.
5. On the corresponding **Enterprise application**, enable **Assignment required**.

The gateway validates the token's signature, issuer, audience and expiry, requires
the `Sandbox.Access` role, and uses the token's object id as the session owner.

### Client/agent registration

1. Register a separate single-tenant application for the trusted caller (no redirect URI for a daemon).
2. Under **API permissions > My APIs**, add the **Application permission** `Sandbox.Access` and grant admin consent.
3. Create a client secret (or certificate); store the secret **value** in `examples\.env` as `MCP_CLIENT_SECRET`.

Role assignment to additional principals is described in the
[pilot guide](pilot-guide.md#admins-assign-the-app-role).

## 4. Deploy the pool

From `infra\pool`, run the infrastructure stages in order (each runs what-if, validate, deploy;
add `-WhatIfOnly` to stop after what-if):

```powershell
cd infra\pool
.\deploy-pool-infra.ps1 -Stage preflight
.\deploy-pool-infra.ps1 -Stage network
.\deploy-pool-infra.ps1 -Stage identity
.\deploy-pool-infra.ps1 -Stage state
.\deploy-pool-infra.ps1 -Stage dns
.\deploy-pool-infra.ps1 -Stage rbac
.\deploy-pool-infra.ps1 -Stage environment
```

Then generate the PKI and the approved policy base, and deploy the apps:

```powershell
# PKI (outputs under .azure\pool-pilot\pki, git-ignored)
.\.venv\Scripts\python.exe infra\pool\make_pki.py
# Offline policy base for the digest-pinned worker + sidecar images (needs az confcom + Docker; run az acr login first)
.\.venv\Scripts\python.exe infra\pool\make_policy_base.py --worker-image <registry>/<image>@sha256:<digest> `
    --sidecar-image <registry>/<sidecar>@sha256:<digest> --pull-identity <id-pool-acrpull resource id> `
    --registry <registry>.azurecr.io --output .azure\pool-pilot\policy-base.json
# Gateway + controller apps
cd infra\pool
.\deploy-pool-apps.ps1 -Stage ingress-dns
.\deploy-pool-apps.ps1 -Stage material
.\deploy-pool-apps.ps1 -Stage build
.\deploy-pool-apps.ps1 -Stage apps -ImageDigest <digest printed by the build stage> -DesiredReady 2 -MaxTotal 3
```

The sidecar (`mcr.microsoft.com/aci/skr`) is used only to produce attestation reports; no
key is released and no HSM or Key Vault is required.

If the worker source changes, rebuild and push the worker image, regenerate the policy
base against the new digest, and redeploy; the controller refuses a policy that does
not match the approved base.

## 5. Make the gateway reachable and trusted

Clients connect over the VPN or from inside the VNet. The environment's internal
ingress presents a **self-signed** certificate that clients pin:

```powershell
python examples\pin_gateway_cert.py https://pool-gateway.<env-domain>/mcp --out gateway-cert.pem
```

Publish the printed SHA-256 fingerprint over a trusted channel; users re-run with
`--expect <sha256>`. VPN clients need the private DNS zone for the environment
domain to resolve (see [infra\pool\README.md](../infra/pool/README.md)).

## 6. Validate

Run an end-to-end analysis from a VPN-connected workstation, after setting
`POOL_GATEWAY_URL` and `POOL_GATEWAY_CA` in `examples\.env`:

```powershell
python examples\pooled_analysis.py
```

It creates a session, waits until active, uploads a spreadsheet, runs pandas,
downloads `summary.csv` and deletes the session in a `finally` block. A worker
destroyed after use is expected; check `rg-confidential-pool` for leftovers.

## 7. Onboard people without distributing application credentials

- **Shared agent (current model):** a trusted front-end authenticates each person and calls
  the gateway as its service principal. Keep the secret in the front-end, never in a user's hands.
  The gateway enforces that a session is used only by the identity that created it.
- **Direct user sign-in:** assign the `Sandbox.Access` role to the user or a group and have the
  client acquire a token for `api://<API_APP_ID>`. Signing in with `az login` additionally
  requires pre-authorizing Azure CLI on the API app, which is not configured in the pilot.
- **VPN:** an Entra-authenticated point-to-site VPN uses the Azure VPN Client and the user's
  Entra sign-in; no personal VPN certificates. VPN access does not by itself grant API access.

## Before production

- Replace the self-signed gateway certificate with a CA-issued one, or keep pinning with a rotation plan.
- Load- and failure-test toward 10 ready / 20 maximum workers; recheck confidential-core quota and capacity.
- Decide how SMT residual risk, the pull-only registry identity, and gateway administrator trust fit the data sensitivity.
- Define monitoring, budget alerts, and a runbook for stuck or leaked worker groups.

## References

- [Pooled design](isolated-session-pool.md), [architecture](ARCHITECTURE.md), [costs](session-pool-costs.md)
- [Confidential containers on ACI](https://learn.microsoft.com/azure/container-instances/container-instances-confidential-overview)
- [Microsoft Azure Attestation](https://learn.microsoft.com/azure/attestation/overview)
- [Microsoft Entra app roles](https://learn.microsoft.com/entra/identity-platform/howto-add-app-roles-in-apps)
