# Getting started

Choose a path: **consume an existing service** from an on-premises notebook/agent,
or **deploy the service** as an administrator. Examples do not create networking
or deploy Container Apps, but session allocation can provision billable workers.
Only synthetic data is appropriate for these examples.

## Consume an existing service

Ask the operator for the private MCP URL, approved TLS certificate fingerprint,
sandbox API application ID, and access to an approved application identity with
`Sandbox.Access`. Establish private routing and DNS from your machine first.
For the agent example, also obtain your Foundry project endpoint, model
deployment name, and inference permission.

### Install from the repository root

Python 3.11+ is required. PowerShell examples below keep the repository root as
the working directory.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r examples\requirements-notebooks.txt
Copy-Item examples\.env.example examples\.env
```

On Microsoft-managed devices where public PyPI is blocked, use the approved proxy:

```powershell
.\.venv\Scripts\python.exe -m pip install --index-url https://packagefeedproxy.microsoft.io/pypi/simple -r examples\requirements-notebooks.txt
```

For scripts without Jupyter, install `examples\requirements-agent.txt` or
`examples\requirements.txt` for the non-agent client. The Agent Framework
distributions are pinned to the offline-checked compatible API set; they are not
worker dependencies.

### Verify and pin gateway TLS

The internal ingress presents the Container Apps environment's platform-managed
certificate (issued by a Microsoft TLS CA, about 100 days of validity, renewed by the
platform). Obtain the expected
SHA-256 fingerprint from the operator **over a trusted channel**, then run:

```powershell
.\.venv\Scripts\python.exe examples\pin_gateway_cert.py https://pool-gateway.REPLACE_DOMAIN/mcp `
    --expect REPLACE_OPERATOR_SHA256 --out examples\gateway-cert.pem
```

The operator may run without `--expect` from a trusted network to initially
publish the fingerprint. Blindly downloading and trusting a certificate does
not authenticate an unknown server. Never use `verify=False`; after rotation,
obtain a new trusted fingerprint.

### Configure the client

Fill `examples\.env` using your own values:

| Setting | Purpose |
|---|---|
| `POOL_GATEWAY_URL` | Private HTTPS MCP endpoint |
| `POOL_GATEWAY_CA` | Verified PEM path, e.g. `gateway-cert.pem` |
| `MCP_API_APP_ID` | Sandbox API application GUID, not the caller app ID |
| `MCP_TENANT_ID`, `MCP_CLIENT_ID`, `MCP_CLIENT_SECRET` | Authorized caller app; secret **value**, not secret ID |
| `FOUNDRY_PROJECT_ENDPOINT`, `FOUNDRY_MODEL` | Agent only: Foundry project and model deployment |

Relative file paths resolve from `examples`, whether launched from the root or
examples directory. Existing process environment settings take precedence over
`.env`. Keep secrets, PEM/private keys, and generated notebook outputs out of Git.

## Authentication from on-premises

**MCP:** `ClientSecretCredential` obtains a client-credentials token for
`api://<MCP_API_APP_ID>/.default`. The local HTTP client attaches
`Authorization: Bearer <token>`. The gateway validates it and requires
`Sandbox.Access`; every session is owned by the authenticated Entra principal.
The model never receives this token or the app secret.

**Foundry:** the agent separately uses `DefaultAzureCredential` to access the
Foundry project/model. For local development, sign in with `az login` using an
identity with the required Foundry data-plane permissions (normally Azure AI
User on the applicable project/account scope). The configured model must support
function calling. This identity may differ from the MCP app. Do not confuse a
Foundry token with the sandbox API token.

The example's `MCP_*` variables do not configure `DefaultAzureCredential`:
that credential chain uses Azure Identity's standard settings/local sign-in.
Do not put MCP secrets into `AZURE_*` variables unintentionally. An on-premises
daemon needs its own approved Foundry credential source, not an assumption that
Azure managed identity is available on-premises.

When `MCP_CLIENT_SECRET` is absent, the MCP helper falls back to
`DefaultAzureCredential`. Direct user sign-in requires separate API client
pre-authorization/delegated configuration and user/group role assignment;
`az login` alone does not enable it. The app-secret flow is the supported
walkthrough. For production, prefer certificate credentials or approved
workload federation and documented rotation; certificate auth is not implemented
by these examples.

**Shared identity limitation:** multiple people using one daemon app share
gateway ownership. Keep app credentials in a trusted application; do not distribute
them as individual-user credentials. Per-human authorization/delegation is a
separate application design.

### Run

```powershell
.\.venv\Scripts\python.exe -m jupyter lab
# Open examples\01_mcp_tools.ipynb, then examples\02_agent_framework.ipynb.
# In VS Code, explicitly select .venv as the notebook kernel.

.\.venv\Scripts\python.exe examples\pooled_analysis.py
.\.venv\Scripts\python.exe examples\agent_framework_analysis.py
```

Notebook cells use top-level `await`, not nested `asyncio.run()`. The direct
script writes `examples\summary.csv`; the agent writes `examples\agent_summary.csv`.
The agent/notebooks validate synthetic counts, means and sums. Upload/download
bytes stay in local code; aggregate execution output is sent to Foundry.

## Deploy as an administrator

This is a guided deployment into existing shared infrastructure, not a complete
installer. Follow [infra\pool\README.md](../infra/pool/README.md) for explicit
parameters and stage commands. No example silently selects an original environment.

Prerequisites: Azure CLI, appropriate permissions, an existing VNet with
non-overlapping delegated subnets/private-endpoint subnet, private routing/DNS,
NAT, registry, Log Analytics workspace, confidential ACI regional capacity/quota,
and Docker in Linux-container mode plus `az confcom` for offline CCE generation.
The infrastructure does not create the shared VPN/VNet/resolver/registry.

### Register and authorize the sandbox API

1. Register a single-tenant API app; expose `api://<api-app-id>`.
2. Set `api.requestedAccessTokenVersion` to `2`.
3. Add the enabled app role `Sandbox.Access`, allowing **Applications**.
   Add Users/Groups only when intentionally implementing direct-user access.
4. Enable assignment required on the enterprise application.
5. Register a separate trusted caller app, grant its **application permission**
   `Sandbox.Access` on this API and grant admin consent. Provision its secret
   securely outside Git.

Role assignment administration is in the [operations guide](operations.md).
Deploy shared infrastructure, generate PKI and a policy base for the exact
worker/sidecar digests, then deploy the gateway/controller and configure private
ingress DNS. Approve attestation minimums and residual risks explicitly;
historical observations are not a universal security baseline.

The sidecar produces attestation evidence only. No HSM or Secure Key Release is
required. When releasing new worker source, rebuild/push the worker image,
regenerate the approved CCE policy base, and update pinned digests together.

## Offline checks and live acceptance

Install test/runtime dependencies when preparing a development environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -r pool-control\requirements.txt -r mcp-build-sandbox\requirements.txt -r examples\requirements-notebooks.txt pytest
.\.venv\Scripts\python.exe -m pytest -q tests\test_examples.py tests\test_public_docs.py tests\test_public_infra.py tests\test_pool_control.py tests\test_pool_feasibility.py
```

Offline notebook tests use explicit fake MCP/model boundaries and do not call
Azure. They establish local control flow, schemas and artifacts, not live
confidential execution. Actual acceptance requires a separately approved
[live release gate](operations.md#live-release-gate).
