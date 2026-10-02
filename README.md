# Confidential Code Sandbox (MCP)

A **confidential code-interpreter** an AI agent drives over **MCP**. It gives the
agent isolated Python sessions to write files, `pip install`, and execute code —
like Azure Container Apps *dynamic sessions*, but running inside an **Azure
Container Instances confidential container group** (AMD SEV-SNP TEE) so work is
memory-encrypted, hidden from the host/operator, and provable via **attestation**.

The agent stays outside and calls MCP tools; the code runs in the enclave (a
hardware-isolated, memory-encrypted region enforced by the CPU that not even the
host, hypervisor, or cloud operator can read into or tamper with).

```
  Any MCP client / agent  ──MCP over HTTPS (bearer auth)──▶  ACI Confidential Group (SEV-SNP TEE)
  (Copilot, Foundry, ...)     TLS terminates inside the TEE   ├─ mcp-server (isolated sessions)
                                                              └─ attestation / SKR sidecar
                              Managed HSM ──Secure Key Release (attestation-gated)──┘
```

## Overview (plain language)

**The problem:** AI agents sometimes need to write and run code to do their job.
That code has to run on *some* computer — and if it touches sensitive data, the
worry is *who else can see it while it runs?* On normal cloud servers, the
provider's infrastructure could technically peek at memory. This project gives
the AI a **private, hardware-locked room** to run code in that **nobody — not
even the cloud operator — can see inside**, and that can *prove* it's genuine.

Think of it like hiring a chef you trust in a restaurant you don't:

- **The locked kitchen** = a CPU security feature (a *TEE*) that seals the room.
  Work happens inside; the walls are one-way mirrors. Staff can pass boxes to the
  door but can't see or touch anything inside. *(This is the confidential part —
  memory is encrypted by the hardware.)*
- **A certificate of authenticity** = *attestation*. Before you send in your
  secret recipe, the kitchen hands you a tamper-proof certificate — signed by the
  CPU itself — proving it's the real sealed kitchen running the approved
  equipment, not a fake with hidden cameras.
- **The recipe safe** = a high-security vault (*Managed HSM*) holding the access
  key. It only hands the key over *after* it sees a valid authenticity
  certificate, so the "password" never sits where a snooper could grab it.
- **The chef stays outside** = the AI agent never enters the kitchen. It passes
  orders through a secure slot ("run this code", "install this") and gets results
  back. The actual work happens inside the sealed room.
- **Separate cutting boards per order** = each task gets its own clean workspace
  (a *session*), so one job can't peek at or interfere with another.

**Two trust checks, opposite directions:** the caller proves *who it is* to the
room with a token (**authentication**), and the room proves *it is genuine* to
the caller and the vault with a hardware-signed report (**attestation**). You
need both: without authentication anyone could send code in; without attestation
you might be sending secrets into a fake "secure" room.

**Why not a normal cloud sandbox?** Azure already has a quick code sandbox
(Container Apps dynamic sessions), but it isn't sealed at the hardware level — no
encrypted memory, no authenticity certificate. This trades a little startup speed
(~1–2 min) for that much stronger, provable privacy.

## Key terms

**Enclave** — a protected, isolated region of a computer, created and enforced by
the **CPU hardware**, where code and data are shielded while they run. Nothing
outside it — the operating system, the hypervisor, other programs, the cloud
host, even an admin with full access to the machine — can read or tamper with
what's inside. Unlike normal containers or VMs (isolated *by software*, so
whoever controls that software could peek in), an enclave is guaranteed by the
hardware itself, so you don't have to trust the surrounding software or the
operator. It combines three things: **memory encryption** (the CPU encrypts the
enclave's RAM with a key the rest of the system never sees), a
**hardware-enforced boundary** (the CPU blocks the host from reading or modifying
enclave memory), and **attestation** (the enclave can prove what code is running
inside). In this project the enclave *is* the confidential container group
running on **AMD SEV-SNP** — the same thing referred to elsewhere as the "TEE".

**TEE (Trusted Execution Environment)** — the general term for the hardware
feature that creates an enclave. Here it's AMD SEV-SNP.

**Attestation** — a hardware-signed report proving the enclave is genuine and
running the exact approved code; verified by Microsoft Azure Attestation (MAA)
and used to gate release of the bearer key from the HSM.

## Why this instead of dynamic sessions?

Azure Container Apps dynamic sessions are the turnkey agent code sandbox, but
they use **Hyper-V isolation, not a TEE** — no data-in-use encryption or
attestation. There is currently **no** offering that is both a turnkey code
interpreter *and* SEV-SNP confidential, so this project builds the interpreter on
confidential ACI. Trade-off: ACI groups start in ~1–2 min (not milliseconds).

## Design (single-tenant)

- The **confidential container group is the trust boundary** — one trusted
  user/app. Data in use is hidden from the Azure host; the image is pinned by the
  CCE policy.
- A **session** is an isolated workspace directory with its **own virtualenv**,
  so each agent task gets a clean, separate Python environment. (Sessions are
  isolated from each other by directory/venv, not by a per-session TEE — that is
  sufficient for single-tenant use.)
- The **bearer token is released from managed HSM only after attestation**
  (Secure Key Release), so credentials never exist in host-visible config.

## Layout

| Path | Purpose |
|------|---------|
| `mcp-build-sandbox/server.py` | FastMCP server: sessions + code execution tools |
| `mcp-build-sandbox/Dockerfile` | Python toolchain + MCP server image |
| `deploy/template.json` | Confidential ACI ARM template (`sku: Confidential`) |
| `deploy/parameters.json` | Parameters (Key Vault refs, managed identity, HSM) |
| `deploy/template-vnet.json` | Confidential ACI template for a private (VNet) IP |
| `deploy/parameters-vnet.json` | VNet-variant params (secrets passed as secure params) |
| `deploy/deploy-vnet.ps1` | VNet deploy workflow (private IP, secure params) |
| `deploy/skr-release-policy.json` | SKR key-release policy bound to MAA claims |
| `deploy/deploy.ps1` | Build, push, create SKR key, gen CCE policy, deploy |
| `examples/confidential_sandbox_demo.ipynb` | Service-principal client: upload Excel, compute average |
| `examples/agent_framework_layer2_demo.ipynb` | Agent Framework: safe per-user session routing (Layer 2) |
| `examples/.env.example` | Copy to `.env`; config for both notebooks |

## MCP tools

Session management:
- `create_session` — new isolated session with its own venv (returns an id)
- `list_sessions`, `delete_session`

Per-session work (all take `session_id`):
- `write_file`, `read_file`, `list_dir`
- `run_python` — execute Python in the session venv
- `pip_install` — install packages into the session venv
- `run_command` — arbitrary shell command in the session workspace
- `run_tests` — pytest in the session venv
- `git_clone` — clone a repo into the session workspace

Attestation:
- `get_attestation` — signed SEV-SNP token; the client should verify it (via
  Microsoft Azure Attestation) before trusting results.

## Command model: free_run

The CCE policy pins only the image and its entrypoint (`python server.py`).
Commands the agent requests run as **child processes** of the server, so they are
not blocked by the ACI exec policy. This keeps the interpreter flexible while
preserving the confidential guarantees. Because arbitrary code runs here, also
apply restricted egress, ephemeral sessions, resource limits, and short-lived
tokens.

## Security model

- **Auth**: bearer token released via attestation-gated **Secure Key Release**
  from managed HSM (`MCP_AUTH_KID` + `SKR_*` env). A user-assigned managed
  identity with *Managed HSM Crypto User* performs the release.
- **Transport**: the server terminates **HTTPS inside the enclave** (cert/key from
  Key Vault), so traffic stays encrypted until it reaches the TEE. No App Gateway,
  which would decrypt outside the enclave.
- **Isolation**: per-session workspace + venv; all file paths confined to the
  session directory.
- **Per-session ownership**: only the identity that created a session can access
  it — enforced *inside the attested enclave*, so neither other callers nor
  infrastructure admins can reach another caller's session data.

## Access control: only the session creator

Each session is bound to the caller who created it. Ownership records live in
`WORKSPACE/.owners/<id>`, outside the agent-writable session directory, so code
running in a session cannot tamper with who owns it. Every per-session tool
calls `_require_owned`, which returns `PermissionError` unless the current
caller matches the stored owner. `list_sessions` shows only the caller's own
sessions.

The caller identity is resolved by the auth middleware:

- **Entra mode** (production): set `ENTRA_TENANT_ID` and `ENTRA_AUDIENCE`. Every
  request must carry a valid Entra access token; the caller identity is the
  token's object id (`oid`). Tokens are validated against the tenant JWKS
  (issuer `.../v2.0`, audience `ENTRA_AUDIENCE`).
- **Dev mode** (local): with Entra unset, requests use the shared bearer token
  and the caller identity comes from the `X-Caller-Id` header (default `local`).

### Per-user scoping for a shared agent (`X-User-Id`)

When one agent (a single service principal) serves many end users, every request
from that agent carries the **same** `oid`, so by itself the server cannot tell
users apart. To get server-enforced per-user isolation without per-user tokens,
the agent stamps an **`X-User-Id`** header with its *authenticated* end-user id.
When present, the server folds it into the caller identity (`oid|user`), so each
session is owned by the `(agent, user)` pair and `_require_owned` denies any
cross-user access — even though all requests share one service principal.

This is **Layer 1**: a guardrail against an honest-but-buggy agent. It trusts the
agent to stamp the correct `X-User-Id`; a compromised agent could still lie. The
only cryptographic guarantee is per-user tokens (On-Behalf-Of), where the user's
own `oid` is validated by the enclave. Pair Layer 1 with **Layer 2** (the agent
never lets the model choose a session/user id) — see
`examples/agent_framework_layer2_demo.ipynb`.

Because the ownership check runs inside the SEV-SNP TEE, an Azure operator or
subscription admin cannot read session data even though they manage the
infrastructure. (A tenant Global Admin who can mint tokens for the audience is
the residual trust boundary — mitigate with PIM, separation of duties, audit.)

### Hardening: block operator log access (`--disable-stdio`)

The TEE protects **data in use** (enclave memory) and the in-enclave workspace,
but a container's **stdout/stderr is surfaced to the ACI control plane** — so
anyone with ARM RBAC on the resource (e.g. an admin) can read it via
`az container logs`. That stream carries only request metadata and server logs,
never caller spreadsheet data (which stays in the TLS channel and enclave
memory), but for a strict "admins can't observe the environment" posture you
should close it.

Generate the CCE policy with stdio disabled:

```powershell
az confcom acipolicygen -a .\template-vnet.json -p .\parameters-vnet.json --disable-stdio
```

`acipolicygen` prints a new policy hash. Because the hash is the SEV-SNP
`hostdata` claim, you must then **rotate the SKR key to the new hash and
redeploy**:

1. Put the new hash in `deploy/skr-release-policy.json`
   (`x-ms-sevsnpvm-hostdata`).
2. Create a new key version:
   `az keyvault key create --hsm-name <hsm> --name mcp-auth-token --kty RSA
   --size 2048 --exportable true --policy .\skr-release-policy.json`
3. Redeploy the group (delete + recreate; confidential groups don't update
   stdio settings in place).

Trade-off: with stdio disabled you can no longer use `az container logs` to
debug the server, so do it only after the deployment is confirmed healthy.


## Run locally (no enclave)

```powershell
cd mcp-build-sandbox
pip install -r requirements.txt
$env:MCP_AUTH_TOKEN = "dev-token"        # local shortcut; in ACI this comes from SKR
$env:SANDBOX_WORKSPACE = "$PWD\.workspace"
python server.py                          # http://localhost:8000/mcp
```

Set `MCP_TLS_CERTFILE` / `MCP_TLS_KEYFILE` to serve HTTPS locally.
`get_attestation` and SKR return "unavailable" locally — they need the sidecar
inside a confidential container group.

## Prerequisites & setup

Standing this up end-to-end requires the following. The private-VNet variant
(`deploy/*-vnet*`) is what the shipped deployment uses.

### Tooling
- **Azure CLI** with the confidential-containers extension:
  `az extension add --name confcom`.
- **Docker** running locally — `az confcom acipolicygen` pulls and hashes the
  image layers. `docker login <acr>.azurecr.io` first (ACR admin creds).
- Permissions to create Entra app registrations and assign Managed HSM roles.

### Azure resources (provision once)
| Resource | Purpose |
|----------|---------|
| **Azure Container Registry** | holds the sandbox image |
| **Managed HSM** (Active, role-assignable) | holds the attestation-gated SKR key |
| **User-assigned managed identity** | the group's identity; releases the key |
| **VNet + delegated subnet** | subnet delegated to `Microsoft.ContainerInstance/containerGroups` |
| **NAT gateway + public IP** | outbound egress (MAA, Entra, ACR, HSM) for the private group |

### Entra ID (two app registrations)
1. **API app** (the sandbox's audience):
   - Expose an app role **`Sandbox.Access`** (member type: applications).
   - Set `requestedAccessTokenVersion = 2`. With v2 the access-token `aud` is the
     **app's GUID** (not the `api://` URI) — so `ENTRA_AUDIENCE` must be the GUID.
   - `identifierUri` = `api://<api-app-id>`; clients request scope
     `api://<api-app-id>/.default`.
2. **Client app** (the on-prem agent's service principal):
   - Granted the `Sandbox.Access` app role on the API app (admin-consented).
   - Create a client secret (or, preferred, a certificate) for the
     client-credentials flow.

### Managed HSM roles (local RBAC, not Azure RBAC)
- The **managed identity** needs **Managed HSM Crypto User** at `/keys`
  (Crypto User has `keys/release`; **Crypto Officer does not** — common pitfall).
- The **operator** creating the key needs **Crypto User** at `/keys` too
  (`keys/create`). Local RBAC takes a few minutes to propagate.

### Key gotchas baked into the config
- **`requirements.txt` pins `mcp<2`** — mcp 2.x removed `FastMCP`, which the
  server imports.
- **`maaEndpoint` and `akvEndpoint` must be hostnames only** (no `https://`).
  The SKR sidecar prepends the scheme itself; a full URL becomes
  `https://https//…` and DNS-fails.
- Every non-secret container **env value and the image are pinned into the CCE
  policy hash**. Finalize them *before* `acipolicygen`; any change means
  regenerate the policy → rotate the SKR key to the new `hostdata` → redeploy.

## Deploy to confidential ACI

### Variant: private VNet (recommended, used here)

`deploy/deploy-vnet.ps1` is the reference workflow. It passes the registry
password and TLS cert/key as **secure parameters** (so a locked-down Key Vault
isn't required) and injects the group into your existing delegated subnet:

1. Build and push the image to ACR.
2. Create a self-signed (or CA-signed) TLS cert for the in-enclave HTTPS.
3. Fill in `deploy/parameters-vnet.json` (subnet id, image, MI resource id,
   `maaEndpoint`/`akvEndpoint` **hostnames**, `entraTenantId`, `entraAudience`
   GUID, `mcpAuthKid`).
4. `az confcom acipolicygen -a template-vnet.json -p parameters-vnet.json`, then
   copy the printed hash into `deploy/skr-release-policy.json`
   (`x-ms-sevsnpvm-hostdata`).
5. Create the exportable SKR key with that release policy.
6. `az deployment group create … --template-file template-vnet.json` (pass
   `registryPassword`, `tlsCert`, `tlsKey` at deploy time).

The group gets a **private IP**; reach it from inside the VNet or over the VPN
gateway. See `examples/confidential_sandbox_demo.ipynb` for a service-principal
client that uploads a spreadsheet and computes an average.

### Variant: public / Key Vault refs

Edit the variables at the top of `deploy/deploy.ps1`, put the CCE policy hash in
`deploy/skr-release-policy.json`, then run the script. It builds the image,
creates the attestation-gated HSM key, generates the CCE policy with
`az confcom acipolicygen`, and deploys the group. The HTTPS MCP endpoint is
printed at the end.

Point your agent's MCP client at `https://<ip>:8000/mcp` and send the bearer
token in the `Authorization` header. Prefer private networking when the agent is
Azure-hosted.

For the strictest posture, regenerate the policy with `--disable-stdio` once the
group is healthy — see **Hardening: block operator log access** above.
