# Confidential Code Sandbox (MCP)

A **confidential code-interpreter** an AI agent drives over **MCP**. It gives the
agent separate Python workspaces to write files, `pip install`, and execute code —
like Azure Container Apps *dynamic sessions*, but running inside an **Azure
Container Instances confidential container group** (AMD SEV-SNP TEE) so work is
memory-encrypted, hidden from the host/operator, and provable via **attestation**.

The agent stays outside and calls MCP tools; the code runs in the enclave (a
hardware-isolated, memory-encrypted region enforced by the CPU that not even the
host, hypervisor, or cloud operator can read into or tamper with).

**Scope:** the TEE protects the container group's memory from the infrastructure
host. It does not make arbitrary code inside that group safe, isolate mutually
untrusted users from one another, or protect data after an authorized caller
receives it. This is a single-trust-domain prototype, not a multitenant sandbox.

**Getting started:** [Private-network deployment and user onboarding](docs/getting-started.md)
covers VNet/VPN prerequisites, service certificates, Entra app registrations,
deployment, and the distinction between application credentials and user sign-in.

```
  Any MCP client / agent  ──MCP over HTTPS (bearer auth)──▶  ACI Confidential Group (SEV-SNP TEE)
  (Copilot, Foundry, ...)     TLS terminates inside the TEE   ├─ mcp-server (session workspaces)
                                                              └─ attestation / SKR sidecar
                              Managed HSM ──Secure Key Release (attestation-gated)──┘
```

## Overview (plain language)

**The problem:** AI agents sometimes need to write and run code to do their job.
That code has to run on *some* computer — and if it touches sensitive data, the
worry is *who else can see it while it runs?* This project gives the AI a
**hardware-protected room** whose memory is encrypted against infrastructure-host
access, and which can provide hardware-signed evidence about its configuration.
Authorized callers and code already inside the room are still trusted.

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
- **Separate cutting boards per order** = each task gets its own workspace
  (a *session*). They organize jobs but are not locked rooms: arbitrary code in
  the same container can still reach another workspace.

**Two trust checks, opposite directions:** the caller proves *who it is* to the
room with a token (**authentication**), and the room proves *it is genuine* to
the caller and the vault with a hardware-signed report (**attestation**). You
need both: without authentication anyone could send code in; without attestation
you might be sending secrets into a fake "secure" room.

**Why not a normal cloud sandbox?** Container Apps dynamic sessions provide
managed, Hyper-V-isolated execution environments, but their documented API does
not expose an attested confidential-computing boundary or Secure Key Release.
Confidential ACI adds that capability, at the cost of more operational work.
It does not replace dynamic sessions' per-session execution isolation.

## Key terms

**Enclave** — a protected, isolated region of a computer, created and enforced by
the **CPU hardware**, where code and data are shielded while they run. Nothing
outside it — the operating system, the hypervisor, other programs, the cloud
host, even an admin with full access to the machine — can read or tamper with
what's inside. Unlike normal containers or VMs (isolated *by software*, so
whoever controls that software could peek in), an enclave is guaranteed by the
hardware itself, so you don't have to trust the surrounding software or the
operator for access to protected memory. It combines three things: **memory encryption** (the CPU encrypts the
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

**This is not generally a better sandbox.** Azure Container Apps dynamic sessions
are the stronger default for executing untrusted or LLM-generated code with
managed, per-session isolation. This project addresses an additional requirement:
an attested confidential-computing boundary protecting data in use from the
infrastructure host.

Here, "Container Apps sandbox" means **dynamic sessions**, not a regular
Container App. There are two session-pool options: a built-in code interpreter,
and custom containers for your own runtime.

| Requirement | Built-in dynamic sessions | Custom-container dynamic sessions | This confidential ACI prototype |
|---|---|---|---|
| Python analysis and spreadsheet processing | Code execution plus file upload/download APIs | Supply your own interpreter and file APIs | MCP tools for Python and file upload/download |
| Custom libraries, binaries, git, or build tooling | Limited to the built-in runtime/API; check required packages | Bring your own image and toolchain | Bring your own image; Python, git, bash, and build tools included |
| Isolation of untrusted code between sessions | A separate Hyper-V isolation boundary per session | A separate Hyper-V isolation boundary per session | Directories and virtualenvs share one container's permissions; **not a security boundary** |
| Protect data in use from the infrastructure host | Hyper-V isolation; not documented as an attested confidential-computing TEE | Same | AMD SEV-SNP encrypted memory within the confidential group |
| Hardware attestation and attestation-gated key release | Not exposed as a documented session-pool capability | Same | MAA attestation, CCE policy, and Managed HSM Secure Key Release |
| Startup and scaling | Prewarmed pools, allocation in milliseconds, managed scaling | Prewarmed pools and managed scaling | Container-group startup takes longer; no managed session pool/scaler in this repo |
| Session lifecycle | Configurable idle cleanup managed by the platform | Managed lifecycle and session-stop APIs | Explicit deletion or container restart; no automatic per-session idle cleanup |
| Execution duration | Documentation currently specifies a 220-second execution limit | Defined by your runtime and service constraints | Default command timeout is 600 seconds; configurable |
| Authentication and user routing | Entra + pool-level roles; your app must protect session identifiers | Same | Entra + request-level ownership checks; trusted code must bind users to sessions |
| Networking | Outbound requests blocked by default; configurable | Network controls available; assess your private-connectivity requirements | Private VNet endpoint and NAT egress; outbound restrictions must be configured separately |
| Agent/MCP integration | Wrap the REST API in a trusted agent tool or MCP adapter | Host your own API/MCP implementation; routing integration is still required | MCP server included |
| Operations and cost | Managed service; assess session-pool pricing | Managed pool plus maintenance of your image | Maintain ACI, image, policies, TLS, HSM, networking, cleanup, and capacity; compare total cost, not just compute |

**Choose dynamic sessions** if the missing capability is custom packages, shell
tools, a custom interpreter, or fast disposable sessions. Consider its
custom-container option before building an execution platform yourself.
Neither option removes the need for application-level user authorization,
protected session identifiers, or controls on the data returned to an LLM.

**Choose confidential ACI** when protecting memory from the infrastructure host,
verifying the workload, or releasing keys only to an attested workload is a hard
requirement. If you also need isolation between mutually untrusted users, this
repo needs an additional execution boundary, such as separate confidential
groups per trust domain. That provisioning/scaling layer is not implemented here.

**Neither design covers the whole agent workflow.** Uploaded data originates
outside the execution environment, and results leave it. In the Agent Framework
demo, the Azure OpenAI model receives tool results; keeping the calculation in a
TEE does not keep those results out of the model service. TLS, egress policy,
authorization, logging, retention, and trusted deployment/key-policy administration
remain separate responsibilities.

Comparison based on Microsoft documentation reviewed on 2026-10-12:
[dynamic sessions](https://learn.microsoft.com/azure/container-apps/sessions),
[code interpreter](https://learn.microsoft.com/azure/container-apps/sessions-code-interpreter),
[custom containers](https://learn.microsoft.com/azure/container-apps/sessions-custom-container),
[session security and routing](https://learn.microsoft.com/azure/container-apps/sessions-usage),
and [confidential ACI](https://learn.microsoft.com/azure/container-instances/container-instances-confidential-overview).

## Design (single-tenant)

- The **confidential container group is the trust boundary** — one trusted
  user/app. Data in use is hidden from the Azure host; the image is pinned by the
  CCE policy.
- A **session** is an isolated workspace directory with its **own virtualenv**,
  so each agent task gets a clean Python environment. This is organizational
  separation, not OS-enforced confinement of arbitrary Python or shell code.
- The **SKR key is released from managed HSM only after attestation**.
  Production caller authentication uses Entra tokens; the released key is used
  for the shared-bearer mode. TLS material and client credentials are provisioned
  separately and are not automatically protected by SKR.

## Layout

| Path | Purpose |
|------|---------|
| `mcp-build-sandbox/server.py` | FastMCP server: sessions + code execution tools |
| `mcp-build-sandbox/Dockerfile` | Python toolchain + MCP server image |
| `infra\template.json` | Confidential ACI ARM template (`sku: Confidential`) |
| `infra\parameters.json` | Parameters (Key Vault refs, managed identity, HSM) |
| `infra\template-vnet.json` | Confidential ACI template for a private (VNet) IP |
| `infra\parameters-vnet.json` | VNet-variant params (secrets passed as secure params) |
| `infra\deploy-vnet.ps1` | VNet deploy workflow (private IP, secure params) |
| `infra\require-tls-policy.ps1` | Finalize multiline-required TLS rules and print the final policy hash |
| `infra\*.example.json` | Sanitized templates, parameters, and release policy; copy to local `.json` files |
| `infra\skr-release-policy.json` | SKR key-release policy bound to MAA claims |
| `infra\deploy.ps1` | Build, push, create SKR key, gen CCE policy, deploy |
| `examples/confidential_sandbox_demo.ipynb` | Service-principal client: upload Excel, compute average |
| `examples/agent_framework_layer2_demo.ipynb` | Agent Framework: safe per-user session routing (Layer 2) |
| `examples/.env.example` | Copy to `.env`; config for both notebooks |
| `examples\requirements.txt` | Notebook dependencies, separate from the container server dependencies |

### Initialize a fresh checkout

Deployment-specific JSON files and `.env` remain ignored by Git. Sanitized
`.example.json` files and `.env.example` are included instead. From the repository
root, create local copies **without overwriting existing configuration**:

```powershell
Get-ChildItem .\infra\*.example.json | ForEach-Object {
    $target = $_.FullName -replace '\.example\.json$', '.json'
    if (-not (Test-Path -LiteralPath $target)) {
        Copy-Item -LiteralPath $_.FullName -Destination $target
    }
}
if (-not (Test-Path .\examples\.env)) {
    Copy-Item .\examples\.env.example .\examples\.env
}
```

Replace all `REPLACE_*` values and configure the deployment scripts to match.
The templates intentionally start with an empty `ccePolicy`: **generate and
finalize the policy, then bind SKR to its final hash before deploying**. Do not
deploy the blank example policy. The examples are starting points, not provisioned
infrastructure or a one-command deployment.

## MCP tools

Session management:
- `create_session` — new isolated session with its own venv (returns an id)
- `list_sessions`, `delete_session`

Per-session work (all take `session_id`):
- `write_file`, `read_file`, `list_dir`
- `upload_file`, `download_file` — binary files as base64
- `run_python` — execute Python in the session venv
- `pip_install` — install packages into the session venv
- `run_command` — arbitrary shell command in the session workspace
- `run_tests` — pytest in the session venv
- `git_clone` — clone a repo into the session workspace

Attestation:
- `get_attestation` — signed SEV-SNP token; the client should verify it (via
  Microsoft Azure Attestation) before trusting results.

## Command model: free_run

The CCE policy pins the image, entrypoint (`python server.py`), and configured
container constraints, including environment rules.
Commands the agent requests run as **child processes** of the server, so they are
not blocked by the ACI exec policy. This keeps the interpreter flexible while
preserving the confidential guarantees. Because arbitrary code runs here, also
apply restricted egress, ephemeral sessions, resource limits, and short-lived
tokens.

## Security model

- **Caller auth**: Entra access tokens in production; shared bearer authentication
  in local/dev mode. The user-assigned managed identity with *Managed HSM Crypto
  User* performs attestation-gated key release (`MCP_AUTH_KID` + `SKR_*` env).
- **Transport**: the server terminates **HTTPS inside the enclave** (cert/key from
  secure parameters for the private deployment, or Key Vault references for the
  public variant), so traffic stays encrypted until it reaches the TEE. No App Gateway,
  which would decrypt outside the enclave.
- **File API scope**: file tools validate paths against the session directory.
  Executed Python/shell code is not subject to that path check.
- **Per-session ownership**: session-scoped MCP tool requests check the identity
  that created the session. This does not confine arbitrary code running under
  the shared container account.
- **Trusted administration**: deployment configuration, TLS provisioning, HSM
  release policies, and client credentials remain trusted responsibilities.
  The TEE does not prevent an authorized administrator from changing future
  deployment or key-release configuration.

## Access control: only the session creator

Each session is bound to the caller who created it. Ownership records live in
`WORKSPACE/.owners/<id>`, outside the session directory used by the file tools.
Every per-session tool
calls `_require_owned`, which returns `PermissionError` unless the current
caller matches the stored owner. `list_sessions` shows only the caller's own
sessions.

These are request-level authorization checks. Since `run_python` and
`run_command` execute with the container's shared permissions, their code can
access paths beyond its working directory, including ownership records and other
session directories. Do not treat these checks as containment for hostile code.

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

The SEV-SNP TEE protects the current group's memory from the infrastructure host.
That guarantee is distinct from user authorization and from control of future
deployments. Protect Entra identities, TLS keys, deployment permissions, and HSM
release-policy administration with least privilege, separation of duties, and
audit. Treat all code running within one group as belonging to the same trust
domain.

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
az confcom acipolicygen -a .\template-vnet.json -p .\parameters-vnet.json --approve-wildcards --disable-stdio
.\require-tls-policy.ps1
```

`require-tls-policy.ps1` prints the final policy hash. Because the hash is the SEV-SNP
`hostdata` claim, you must then **bind SKR release to the new hash and
redeploy**:

1. Put the new hash in `infra\skr-release-policy.json`
   (`x-ms-sevsnpvm-hostdata`).
2. Update the existing key's mutable release policy, or create a new key version:
   `az keyvault key create --hsm-name <hsm> --name mcp-auth-token --kty RSA
   --size 2048 --exportable true --policy .\skr-release-policy.json`
3. Redeploy the group, planning for a restart and loss of ephemeral sessions.
   If Azure requires deletion/recreation, perform it only after approving that
   interruption.

Trade-off: with stdio disabled you can no longer use `az container logs` to
debug the server, so do it only after the deployment is confirmed healthy.


## Run the example notebooks

From the repository root, create the local environment and install notebook
dependencies (not `mcp-build-sandbox\requirements.txt`):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --index-url https://packagefeedproxy.microsoft.io/pypi/simple -r .\examples\requirements.txt ipykernel
az login
```

Reuse an existing `.venv` rather than recreating it. The index URL is required on
Microsoft-managed devices; on an unrestricted device, it can be omitted.
In VS Code, select `.venv\Scripts\python.exe` as the notebook kernel.

Configure `examples\.env`:

| Setting | Value |
|---|---|
| `MCP_TENANT_ID` | Tenant owning the app registrations |
| `MCP_CLIENT_ID` | Application ID of the **client/agent app** |
| `MCP_CLIENT_SECRET` | Secret **Value** from that client app, not the API app and not the Secret ID |
| `MCP_API_APP_ID` | Application ID GUID of the **sandbox API app**; used to request `api://<id>/.default` |
| `MCP_ENDPOINT` | Current sandbox URL, `https://<private-ip>:8000/mcp` |
| `AZURE_OPENAI_ENDPOINT` | Agent Framework only: `https://<resource>.openai.azure.com/`, **not** a Foundry `/api/projects/...` URL |
| `AZURE_OPENAI_CHAT_DEPLOYMENT` | Agent Framework only: model deployment name on that Azure OpenAI resource |

The basic sandbox notebook needs only the MCP settings. The Agent Framework
notebook additionally authenticates to Azure OpenAI using
`DefaultAzureCredential`; its local user needs model-inference permissions.
The sandbox service-principal secret is not reused for that model call.

Run notebook cells from the top. Local `.env` settings take precedence over
existing kernel environment variables. After changing `.env`, rerun configuration
and client/agent construction, or restart the kernel and run all cells. Existing
clients and agents retain the settings with which they were created.

For a private deployment, connect through a VPN or another network path into the
VNet. An Entra-authenticated point-to-site gateway uses Azure VPN Client and its
gateway-specific downloaded profile; your normal corporate VPN is not enough
unless it routes to this VNet. The container must be running. Its private IP can
change after a restart or redeployment; check it with:

```powershell
az container show -g <resource-group> -n <container-group> --query ipAddress.ip -o tsv
Test-NetConnection <private-ip> -Port 8000
```

Update `MCP_ENDPOINT` when the address changes. A successful TCP test does not
prove HTTPS is configured: `SSL: WRONG_VERSION_NUMBER` can indicate a server
serving plain HTTP. Repair server-side TLS rather than sending tokens over HTTP.

## Run the server locally (no enclave)

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install --index-url https://packagefeedproxy.microsoft.io/pypi/simple -r .\mcp-build-sandbox\requirements.txt
$env:MCP_AUTH_TOKEN = "dev-token"        # local shortcut; in ACI this comes from SKR
$env:SANDBOX_WORKSPACE = "$PWD\mcp-build-sandbox\.workspace"
python .\mcp-build-sandbox\server.py     # http://localhost:8000/mcp
```

Set `MCP_TLS_CERTFILE` / `MCP_TLS_KEYFILE` to serve HTTPS locally.
`get_attestation` and SKR return "unavailable" locally — they need the sidecar
inside a confidential container group.

### Where the HTTPS certificate comes from

The private deployment takes the HTTPS certificate from `infra\tls\cert.pem`
and its private key from `infra\tls\key.pem`. You supply these files; Azure does
not automatically issue the certificate for this server. The current demo uses
a self-signed certificate. Deployment passes their PEM contents as the
`tlsCert` and `tlsKey` secure parameters, and the server materializes them inside
the enclave for Uvicorn's HTTPS listener. The public variant uses Key Vault
references instead. Certificates and private keys are ignored by Git.

The **TLS certificate is not the attestation token**. TLS protects the connection;
the MAA token supplies evidence about the confidential workload. A self-signed
certificate by itself proves neither the server's identity nor its enclave status.
The notebooks currently disable certificate verification for demo convenience and
do not implement a complete attestation-verification protocol before uploading
data. For production, use a CA-issued certificate for a stable private DNS name
and enable verification, or implement a properly authenticated, attestation-bound
channel. Never assume that fetching an attestation token without validating it
establishes trust.

## Prerequisites & setup

Standing this up end-to-end requires the following. The private-VNet variant
(`infra\*-vnet*`) is what the shipped deployment uses.

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
  regenerate/finalize the policy → bind SKR release to the new `hostdata` → redeploy.

## Deploy to confidential ACI

### Variant: private VNet (recommended, used here)

`infra\deploy-vnet.ps1` is the reference workflow. It passes the registry
password and TLS cert/key as **secure parameters** (so a locked-down Key Vault
isn't required) and injects the group into your existing delegated subnet:

1. Build and push the image to ACR.
2. Create a self-signed (or CA-signed) TLS cert for the in-enclave HTTPS.
3. Fill in `infra\parameters-vnet.json` (subnet id, image, MI resource id,
   `maaEndpoint`/`akvEndpoint` **hostnames**, `entraTenantId`, `entraAudience`
   GUID, `mcpAuthKid`).
4. Run `az confcom acipolicygen -a template-vnet.json -p parameters-vnet.json --approve-wildcards`,
   then `.\require-tls-policy.ps1` from the `infra` directory.
   Copy the **final** hash printed by that script into `infra\skr-release-policy.json`
   (`x-ms-sevsnpvm-hostdata`).
5. Create the exportable SKR key with that release policy.
6. `az deployment group create … --template-file template-vnet.json` (pass
   `registryPassword`, `tlsCert`, `tlsKey` at deploy time).

The group gets a **private IP**; reach it from inside the VNet or over the VPN
gateway. See `examples/confidential_sandbox_demo.ipynb` for a service-principal
client that uploads a spreadsheet and computes an average.

Keep `tlsCert` and `tlsKey` out of `parameters-vnet.json`; supply their PEM values
only as secure parameters at deployment time. `--approve-wildcards` generates
rules for these unresolved secret values without exposing them in the CCE policy.
`require-tls-policy.ps1` makes the TLS rules accept multiline PEM values and
requires both variables, preventing a silent fallback to plaintext HTTP.
Generating the policy with placeholder TLS values instead pins those placeholders,
causing the enclave to drop the real values and the server to start without HTTPS.

### Variant: public / Key Vault refs

The public variant uses `template.json` and `parameters.json`, copied from the
sanitized examples. Configure the resource IDs and Key Vault references, build
and push the image, and provision the referenced registry/TLS secrets first.
Key Vault must permit ARM template deployment and the operator must have the
required deployment access.

`infra\deploy.ps1` is an older reference workflow, not a production-ready
one-command script. In particular, generate/finalize the policy **before** binding
the SKR key to its hash, and keep MAA/HSM container endpoints as hostnames, without
the `https://` overrides in that reference script. From the `infra` directory:

```powershell
az confcom acipolicygen -a .\template.json -p .\parameters.json --approve-wildcards
.\require-tls-policy.ps1 -TemplatePath .\template.json
```

Copy the final hash into `skr-release-policy.json`, create or update the
attestation-bound HSM key, then deploy with `az deployment group create` using
the configured template and parameters. Never deploy the example's blank policy.

Point your agent's MCP client at `https://<ip>:8000/mcp` and send the bearer
token in the `Authorization` header. Prefer private networking when the agent is
Azure-hosted.

For the strictest posture, regenerate the policy with `--disable-stdio` once the
group is healthy — see **Hardening: block operator log access** above.
