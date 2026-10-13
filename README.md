# Confidential Code Sandbox (MCP)

A **confidential code-interpreter** an AI agent drives over **MCP**. The agent
writes files, runs Python (pandas/openpyxl preinstalled) and downloads results,
like Azure Container Apps *dynamic sessions*, but every analysis session runs in
its **own Azure Container Instances confidential container group** (AMD SEV-SNP
TEE). Work is memory-encrypted, hidden from the host/operator, and provable via
**attestation**.

**Design:** one confidential ACI group per session, an Entra-authenticated MCP
gateway, a Table-backed lease store, and a pool controller that keeps a bounded
number of ready workers and destroys each worker after use. See
[the pooled design](docs/isolated-session-pool.md),
[architecture](docs/ARCHITECTURE.md), and [cost estimates](docs/session-pool-costs.md).

**Scope:** the TEE protects a worker's memory from the infrastructure host. It
does not make arbitrary code inside the worker safe, protect data after an
authorized caller receives it, or hide data from the gateway/controller
administrators (the control plane is not confidential). Residual risks (SMT,
pull identity) are listed in the design doc.

**Status:** the pilot (3 sessions total, 2 warm workers) was validated live; the
10-ready/20-maximum pool is not deployed. The Managed HSM, Secure Key Release and
the earlier single-group demo were removed on 2026-10-13
([removal decision](docs/isolated-session-pool.md#removal-decision-2026-10-13)).

**Getting started:** [getting-started.md](docs/getting-started.md) (deploy and
onboard) and the [pilot guide](docs/pilot-guide.md) (use the pilot).

```
  Agent / MCP client ──MCP + Entra token──▶ pool-gateway ──mutual TLS──▶ confidential ACI group (SEV-SNP)
  (VPN or in-VNet)                          │ (Container Apps)            ├─ mcp-server (one session)
                                            └─ Table lease store          └─ attestation sidecar
                                  pool-controller ── create/verify/delete groups ──▶ worker pool
```

## Overview (plain language)

AI agents sometimes need to write and run code, and if it touches sensitive data
the worry is who else can see it while it runs. This project gives each analysis a
**hardware-protected room** whose memory is encrypted against infrastructure-host
access and which can show hardware-signed evidence about its configuration.
Authorized callers and code already inside the room are still trusted.

- **The locked kitchen** = a CPU feature (a *TEE*) that seals the room; memory is
  encrypted by the hardware.
- **A certificate of authenticity** = *attestation*. Before data goes in, the room
  presents a CPU-signed report proving it is the real sealed environment running
  the approved image.
- **One kitchen per order** = each session gets its own worker group, destroyed
  afterwards; a used worker is never reused.
- **The chef stays outside** = the agent never enters; it passes requests through
  the gateway and gets results back.

**Two trust checks, opposite directions:** the caller proves *who it is* with an
Entra token (**authentication**), and the worker proves *it is genuine* with a
hardware-signed report (**attestation**).

## Key terms

**Enclave / TEE (Trusted Execution Environment)** — a protected region of a
computer created and enforced by the CPU, where code and data are shielded while
they run: memory encryption, a hardware-enforced boundary, and attestation. Here
the enclave is the confidential container group on **AMD SEV-SNP**.

**Attestation** — a hardware-signed report proving the environment is genuine and
running the approved code; verified through Microsoft Azure Attestation (MAA). The
pool binds it to each worker's TLS key.

**CCE policy** — the confidential-container policy that pins the exact images and
entrypoint of a worker group; its hash is part of the attestation.

## Why no HSM

Workers attest to the controller via MAA with TLS-key binding, per-worker keys
derive from the controller CA, and no secret is released to workers, so there is
nothing for an HSM or Secure Key Release to gate. A Key Vault Premium SKR path
could be added later if a secret must be released to attested workers. Details:
[Why no HSM](docs/isolated-session-pool.md#why-no-hsm).

## Alternatives: ACA Sandboxes and dynamic sessions

**ACA Sandboxes is not dynamic sessions.** The
[Sandboxes product](https://learn.microsoft.com/azure/container-apps/sandboxes-overview)
provides individual isolated compute environments with prewarmed startup,
suspend/resume, storage, networking controls, and programmable lifecycle. It is a
strong candidate for untrusted-code isolation when confidential computing is not
required.

| Requirement | ACA Sandboxes | Confidential ACI pool (this repo) |
|---|---|---|
| Isolate untrusted analysis sessions | Per-sandbox compute/network boundary | Separate confidential group per analysis |
| Confidential memory and attestation | Not established by the reviewed SandboxGroups documentation | SEV-SNP TEE and MAA/CCE attestation |
| Prewarming and lifecycle | Platform prewarming, suspend/resume, lifecycle policies | Controller-managed ready groups; destroy used groups |
| Custom runtime | OCI-derived disk images | Approved container images |
| Agent endpoint | Wrap sandbox SDK operations in a trusted MCP service | Single trusted MCP gateway and pool controller |

Confidential DC workload profiles for ordinary Container Apps do not establish
that ACA Sandboxes support confidential compute. This repository selects
confidential ACI because confidential computing is required.

### Dynamic sessions comparison

**This is not generally a better sandbox.** Azure Container Apps dynamic sessions
are the stronger default for executing untrusted or LLM-generated code with
managed, per-session isolation. This project addresses an additional requirement:
an attested confidential-computing boundary protecting data in use from the
infrastructure host.

| Requirement | Dynamic sessions (built-in or custom container) | This confidential ACI pool |
|---|---|---|
| Python analysis and spreadsheet processing | Code execution plus file APIs, or your own interpreter | MCP tools for Python and file upload/download |
| Isolation between untrusted sessions | A separate Hyper-V isolation boundary per session | A separate confidential group per session |
| Protect data in use from the infrastructure host | Hyper-V isolation; not documented as an attested confidential TEE | AMD SEV-SNP encrypted memory with MAA attestation |
| Startup and scaling | Prewarmed pools, allocation in milliseconds, managed scaling | Warm workers start a session in about 1 minute; a cold replacement takes about 4 minutes; scaling is this repo's controller |
| Session lifecycle | Platform-managed idle cleanup | Idle 30 min / absolute 4 h, then the group is destroyed |
| Networking | Outbound blocked by default; configurable | Workers have no egress by design (NAT only for MAA/ACR) |
| Agent/MCP integration | Wrap the REST API in a trusted adapter | MCP gateway included |
| Operations and cost | Managed service | Maintain ACI, image, policies, PKI, networking and capacity; see [costs](docs/session-pool-costs.md) |

**Choose dynamic sessions** if the missing capability is custom packages, shell
tools or fast disposable sessions and confidential computing is not required.
**Choose confidential ACI** when protecting memory from the infrastructure host
or verifying the workload is a hard requirement.

**Neither design covers the whole agent workflow.** Uploaded data originates
outside the execution environment and results leave it. If a model consumes tool
results, keeping the calculation in a TEE does not keep those results out of the
model service. TLS, egress policy, authorization, logging and retention remain
separate responsibilities.

Comparison based on Microsoft documentation reviewed on 2026-10-12:
[dynamic sessions](https://learn.microsoft.com/azure/container-apps/sessions),
[code interpreter](https://learn.microsoft.com/azure/container-apps/sessions-code-interpreter),
[custom containers](https://learn.microsoft.com/azure/container-apps/sessions-custom-container),
and [confidential ACI](https://learn.microsoft.com/azure/container-instances/container-instances-confidential-overview).

## Layout

| Path | Purpose |
|------|---------|
| `mcp-build-sandbox\server.py` | Worker FastMCP server: one lifetime session, mutual TLS, file and code-execution tools, attestation |
| `mcp-build-sandbox\pool_worker.py` | Worker lifetime-session state machine, TLS validation, attestation challenge |
| `mcp-build-sandbox\Dockerfile` | Python toolchain + worker image |
| `pool-control\` | Gateway, controller, Table lease store, Azure adapter, PKI material |
| `infra\pool\` | Pool infrastructure (Bicep, deploy scripts, policy generation, smoke test); see its README |
| `examples\pooled_analysis.py` | Client: create session, upload a spreadsheet, run pandas, download, delete |
| `examples\pin_gateway_cert.py` | Pin the gateway's self-signed certificate |
| `examples\pool_feasibility.py` | Two-worker feasibility probe |
| `examples\.env.example` | Copy to `examples\.env`; client configuration |
| `tests\` | Worker and pool-control tests |

Deployment-specific values and `.env` are git-ignored. Local checks:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_pool_feasibility.py tests\test_pool_control.py
```

## MCP tools

Sessions: `create_session`, `get_session_status`, `list_sessions`, `delete_session`.

Per-session work (all take `session_id`):
- `write_file`, `read_file`, `list_dir`
- `upload_file`, `download_file` — binary files as base64
- `run_python`, `run_command`, `run_tests`
- `pip_install`, `git_clone` — exist, but fail because workers have no egress

Attestation: `get_attestation` — signed SEV-SNP/MAA token bound to the worker's
TLS key and a client challenge; the client must verify it.

## Security model

- **Boundary:** the confidential ACI group, one per session. Directories and
  virtualenvs inside a worker are not a security boundary; the worker serves one
  session for its lifetime and is then destroyed.
- **Authentication:** Entra token (API app audience, role `Sandbox.Access`) at the
  gateway; the gateway enforces ownership and the persistent lease on every call.
  Gateway-to-worker traffic uses mutual TLS with per-worker certificates; CA keys
  never enter a worker.
- **Attestation:** the controller verifies fresh MAA evidence against the approved
  CCE policy and SVN minimums before data is allowed in.
- **Network:** workers sit in a dedicated subnet whose NSG only admits the control
  subnet on TCP 8000 and denies worker-to-worker and worker-to-control traffic.
- **Free command model:** commands run as child processes of the worker, so a
  session can run arbitrary code with the worker's permissions; isolation comes
  from the per-session group, not from the process.
- **Residual risks:** SMT enabled on the host, the pull-only registry identity
  reachable from session code, gateway/controller administrators trusted.

## Deploy

Pool infrastructure and apps: [docs/getting-started.md](docs/getting-started.md)
and [infra\pool\README.md](infra/pool/README.md).
