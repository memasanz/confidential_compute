# Confidential code execution over MCP

Run an AI agent **on-premises**, use **Microsoft Foundry for model inference**,
and execute its analysis code in a dedicated **Azure confidential container**.
The agent calls a private MCP gateway; each analysis session gets its own Azure
Container Instances (ACI) confidential container group using **AMD SEV-SNP**.

This is real hardware-backed confidential computing, not just a private network
or an ordinary container. A worker's memory is encrypted against infrastructure
host access, and the controller verifies fresh attestation before admitting it.
Workers are **destroyed after use**, never returned to the ready pool.

**Status: live-validated; sign-off pending.** On **2026-10-24** the `basic` and
`hostile` smoke phases passed against the deployed build (per-session encrypted
channel, two-caller cross-owner denial, hostile-code limits). The pool runs a
configured **3-worker maximum / 2-ready target**. Three requirements are
partially met and accepted as residual risks, and the release approval record
needs a human signature; see the
[requirements assessment](docs/requirements-assessment.md). A 10-ready /
20-maximum pool remains an unvalidated scale target. See the
[operations guide and live release gate](docs/operations.md).

**Pool sizing:** `POOL_MAX_TOTAL` is a configurable application limit, not a
network limit. The code permits up to **20 total workers** and a ready target
of up to **10**, subject to Azure quota, subnet space and regional availability.
With a 3-worker cap and 2-ready target, one active worker can coexist with two
ready workers, but three active workers leave no ready reserve. Provisioning
and deleting workers also count toward the cap.

**Deployment readiness:** another team can follow the
[deployment guide](infra/pool/README.md) to deploy it in your own existing
Azure VNet. This is a guided setup, not a turnkey installer: shared networking,
private routing/DNS, registry, identity and security approvals must be supplied.
Fresh-environment acceptance, persistent controller material storage,
certificate rotation, failure recovery, scale and startup latency still need
production hardening.

## How it works

Think of this as a **confidential code interpreter for an on-premises AI agent**.
For example, when you ask the agent to analyze a spreadsheet:

1. **The agent calls a Foundry model** to decide what analysis/code to run.
   The agent loop itself stays on-premises.
2. **Local code authenticates to the private MCP gateway** using Entra and
   requests an analysis session.
3. **The pool assigns a dedicated confidential worker.** Its memory is
   protected using AMD SEV-SNP; the controller verifies attestation before
   admitting the worker.
4. **The spreadsheet is uploaded and Python runs in that worker.** Analysis
   summaries can return to the model; local code downloads the resulting files.
5. **The session is released and its worker is destroyed.** A used worker is
   never handed to another session. The controller provisions fresh replacements
   as needed to restore the ready target within the total cap.

A session can acquire an already-running, unused warm worker; it does not
necessarily wait for a new group to be provisioned. That worker belongs to
one session for its lifetime, across multiple code executions and tool calls.
Additional requests queue when no worker is ready.

**Only worker execution is confidential.** The gateway can see routed data,
and Foundry can see any tool results sent to the model. The entire agent/model
workflow is not inside confidential compute.

## Try the examples

| Example | What it demonstrates |
|---|---|
| [01 - MCP tools notebook](examples/01_mcp_tools.ipynb) | Discover tools, allocate a confidential session, transfer files, execute pandas, verify a downloaded CSV, release |
| [02 - Agent Framework notebook](examples/02_agent_framework.ipynb) | A locally running agent uses a Foundry model and session-bound MCP tools |
| [Runnable Agent Framework code](examples/agent_framework_analysis.py) | The same on-premises integration without Jupyter |
| [Direct Python client](examples/pooled_analysis.py) | Deterministic spreadsheet analysis without a model |

The four-row `sample_sales.xlsx` is synthetic: East/West/North/South,
revenue 120/200/150/170 and units 12/20/15/17. Examples verify totals of
**640 revenue and 64 units**, rather than trusting a model's success claim.

You need a deployed service, private routing (VPN/ExpressRoute or equivalent),
working private DNS, a verified gateway certificate, and an Entra app with
`Sandbox.Access`. The agent example also needs a Foundry project/model and
separate inference permissions. **The notebooks do not deploy infrastructure.**

From the repository root, after creating a Python 3.11+ virtual environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -r examples\requirements-notebooks.txt
Copy-Item examples\.env.example examples\.env
# Fill in your own configuration; never commit .env or certificates.
.\.venv\Scripts\python.exe -m jupyter lab
# Or run without Jupyter:
.\.venv\Scripts\python.exe examples\agent_framework_analysis.py
```

On managed devices, use your approved package index. Complete instructions:
[getting started](docs/getting-started.md). For an existing service:
[operations guide](docs/operations.md). For deployment:
[infrastructure guide](infra/pool/README.md).

## Architecture and confidential boundary

![On-premises agent and confidential session pool](docs/images/architecture.svg)

[Architecture details](docs/ARCHITECTURE.md) include a Mermaid diagram and
[editable diagram source](docs/images/architecture.excalidraw).

**Only worker execution is inside the TEE.** The on-premises agent, Foundry model,
gateway, controller, and their administrators remain outside that boundary.
Gateway/controller administrators can see routed data; a model can see tool
results returned to it. This is **not end-to-end confidential inference** and
does not make arbitrary code inside a worker trustworthy.

| Check | What it proves |
|---|---|
| Entra authentication | The caller has API permission; sessions belong to its validated principal |
| Gateway-to-worker mutual TLS | The gateway reaches the authenticated worker |
| MAA attestation + CCE policy | The admitted worker meets the approved image/policy/security baseline, bound to its TLS key and a TEE-generated session public key |
| Per-session encrypted channel | Content tools are reachable only through AES-256-GCM envelopes keyed with a random per-session secret, so the transport TLS key alone cannot read or forge session content |

Using one service principal for multiple people shares gateway ownership. This
sample does not implement per-human delegated authorization.

### Per-session key

Each worker generates an X25519 key pair inside the TEE. Its public key is part
of the attested challenge, so the hardware-signed report binds it to the worker.
The gateway verifies fresh evidence, generates a random 256-bit session key in
its own memory, wraps it to that attested key, and the worker accepts it once.
Content operations then travel as authenticated envelopes with directional
subkeys and a strictly increasing counter (replay resistant). The key is never
persisted; deleting the session or restarting the gateway invalidates it, so
the gateway runs as a single replica.

### Administrator trust model

This design protects session data from the Azure host and from other sessions.
It accepts the **narrower administrative boundary**: workload administrators
(gateway, controller, PKI) are constrained by separately owned Azure Policy and
RBAC, and an independent security authority controls those policies. Azure
administrators who can change the governing policies are handled by that
separation of duties, not by cryptography. The gateway handles plaintext by
design. Tool results and files transit it, so its administrators must be trusted
for the data they route. Protection against *all* Azure administrators would
also need no gateway plaintext, which this design does not implement.

**Secret residency:** the per-session key exists only in gateway and worker
memory. The worker generates its own TLS key inside the TEE (attested TLS). The
key is never in configuration, the environment or controller storage, and the
controller holds no worker signing CA. The gateway accepts the worker only when
MAA evidence commits to the exact TLS key it observed on that connection, and
pins later calls to it, so a substituted key fails admission.

### Worker hardening

- Generated code runs as an unprivileged `sandbox` user with a minimal
  environment (no TLS key, tokens or worker settings) and bounded memory,
  process count and file size. The server keeps `/app` and its own environment
  unreadable to that user.
- The workspace is capped at 2 GiB. Temporary data lives on the confidential
  container's ephemeral filesystem, which sits in encrypted TEE memory and is
  discarded with the group; no volume is mounted. Verify this on a live worker.
- Workers never restart (`restartPolicy: Never`); a restart retires the session
  and a retry gets a new group and new material.
- `infra/pool/orphan_cleanup.py` deletes worker groups past a maximum age using
  ARM tags and start times only, independently of the controller. Run it on a
  schedule (see the infrastructure guide).
- Gateway and worker logs are content-free: no per-request lines, and tool errors
  are not logged.

## Tools and limits

Sessions: `create_session`, `get_session_status`, `list_sessions`, `delete_session`.
Session IDs are generated by the gateway. Every session tool is scoped to the
authenticated caller: `list_sessions` returns only sessions that caller owns, and
status, execution and delete on another caller's session are rejected.
Files: `write_file`, `read_file`, `delete_file`, `list_dir`, `upload_file`, `download_file`.
Execution: `run_python`, `run_command`, `run_tests`.
Evidence and health: `get_attestation`, `get_status`.

The file and execution tools are not plain MCP tools on the worker. The gateway
exposes them to clients as before, but it calls them on the worker only inside
the per-session encrypted channel (`establish_session_key`, then `invoke`), so
a client or network observer never sees a plaintext worker content endpoint.

`pip_install` and `git_clone` exist but fail under the configured egress
restrictions. Bake packages into an approved, digest-pinned worker image instead.
Workers have restricted service egress for attestation/image dependencies, not
general-purpose internet access.

Configured limits: 30-minute idle / 4-hour absolute lifetime, 300-second
operation limit, 1 MiB bounds, and a 10-minute queue wait. JSON/base64 overhead
reduces usable file size below the configured serialized request/result limits.
Warm acquisition was about one minute and cold replacement about four minutes;
these are historical observations, not fixed waits or latency guarantees.
Warm acquisition includes controller scheduling, Azure management checks,
fresh attestation and activation, plus client status polling. Attestation alone
has not been timed separately, so it is not established as the bottleneck.

Accepted risks include SMT-enabled hosts and a pull-only registry identity
reachable from worker code. Because that identity is attached to the worker, its
token endpoint (the instance metadata service) is reachable from generated code; the network
rules cannot block that link-local address. The identity holds only `AcrPull` on
one registry (`acr-pull-only`), but this does not meet a strict "no access to
cloud metadata or management credentials" requirement. There is no HSM or Secure Key Release path; the
attestation sidecar's `skr` image name is historical. See the
[design and rollout gates](docs/isolated-session-pool.md).

Live check (2026-10-24, build with the per-session encrypted channel). The
`basic` phase of `infra/pool/smoke.py` ran for two separate callers: create,
activate, upload, pandas execution, download, delete, rejected cross-owner
status/exec/delete, an empty cross-owner list and rejected use after delete. All
passed through the encrypted channel. The `hostile` phase repeated the findings
below on this build: generated code ran as a non-root user with no secret
environment variables, could not read the server's code or environment, could
not reach the internet, and was stopped by the process, memory and file-size
limits; another session's files were not reachable. The metadata endpoint was
reachable, as described above. Disruptive restart, replay and substitute-worker
injection against the live pool has not been run; those cases are covered by
automated tests. See the [requirements assessment](docs/requirements-assessment.md).

## Repository map

| Path | Purpose |
|---|---|
| `examples\` | Notebooks, on-premises agent, direct client, synthetic spreadsheet |
| `mcp-build-sandbox\` | Single-lifetime-session worker and image |
| `pool-control\` | MCP gateway, controller, lease store, Azure adapter, deployment material |
| `infra\pool\` | Parameterized deployment, CCE policy generation, smoke probe |
| `tests\` | Worker, lifecycle, client, notebook and offline integration checks |
| `docs\` | Architecture, setup, operations, design and [cost estimates](docs/session-pool-costs.md) |

Offline tests use explicit fake services; they do not demonstrate live Azure
availability or attestation. Follow the [release gate](docs/operations.md#live-release-gate)
before claiming a successful deployment.

## When to use this

Choose this design when **attested confidential memory** is a hard requirement.
For ordinary untrusted-code execution without that requirement, evaluate managed
[Container Apps dynamic sessions](https://learn.microsoft.com/azure/container-apps/sessions)
or the separate [ACA Sandboxes product](https://learn.microsoft.com/azure/container-apps/sandboxes-overview).
Neither product is substituted here or assumed to provide this SEV-SNP
attestation guarantee. This design requires you to operate the pool, PKI,
networking, capacity, and lifecycle yourself.
