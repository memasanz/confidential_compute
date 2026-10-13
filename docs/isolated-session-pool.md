# Confidential analysis session pool

## Decision and current status

Use **one Azure Container Instances confidential container group per analysis
session**, behind a single private MCP gateway. Each analysis session has a
separate Hyper-V-isolated AMD SEV-SNP trusted execution environment (TEE).
The group contains the code-execution container and its attestation sidecar.
It is not a group containing multiple mutually untrusted analysis sessions.

This uses the platform described in
[Confidential containers on Azure Container Instances](https://learn.microsoft.com/azure/container-instances/container-instances-confidential-overview).
ACI provides the confidential execution boundary; this project must implement
the gateway, allocation, prewarming, expiry, and cleanup.

**Status:** the worker feasibility slice and the gateway/controller
(`pool-control`) are implemented; the combined local suite passes 53 tests.
Shared infrastructure is deployed (worker and control subnets, restrictive NSG,
Entra-only private Table storage, separate gateway/controller/pull identities,
private DNS, an internal Container Apps environment). The gateway and controller
apps and the 10/20-worker pool are not deployed.

**Live two-worker pilot (October 12, 2026):** two fresh confidential groups
provisioned under finalized CCE policy; each worker's attested host data matched
its policy hash. Network and file separation held: a worker could not reach the
other worker, control, or state (or the since-removed single-group demo); files and session IDs did not
cross workers; retirement rejected all further calls. Both groups were destroyed
with no leftovers. Attestation passed every check except SMT: Azure reports
`smt-allowed=true` on this platform.

**Accepted residual risks and decisions:**

- **SMT enabled.** SEV-SNP does not defend against microarchitectural side
  channels between threads sharing a core. This is accepted as a documented
  residual risk, applied via explicit `POOL_ALLOW_SMT=true`. It weakens the
  mutually-untrusted-code guarantee against a highly sophisticated co-resident
  attacker; it is not controllable by this project.
- **Pull identity.** Azure Container Instances needs an attached identity to
  pull from the private registry. The pull-only identity `id-pool-acrpull` has
  AcrPull on that one registry. Code in a session can obtain a token for it.
  This reverses the earlier "no runtime identity" goal and is accepted.
- **SVN minimums** are set from observed live values (bootloader 10, tee 0,
  snpfw 23, microcode 84, guest 2, uvm_guest 104), so a platform downgrade fails
  closed.

**Gateway/controller pilot (October 13, 2026):** both apps are deployed as
internal Container Apps (`pool-gateway`, `pool-controller`) with the pilot cap
(3 total, 2 ready), now scaled to zero ready workers to stop spend. An in-VNet
smoke test passed create, spreadsheet upload, pandas analysis, download, delete,
and cross-owner denial. Capacity behavior did not fully pass: with 4 sessions
against 2 ready workers, the 2 queued sessions expired before replacements
were ready.

Follow-up fixes (October 13, 2026): the slow deletes were not IPv6. ACI deletes
take 6-67 s and the SDK timeout was too short; timeouts are now 120 s and the
controller works through workers concurrently. Worker keys are no longer stored:
each is derived deterministically from the controller-held RSA CA key, so a
controller restart reproduces the same certificate. Measured provisioning to
ready is about 3.5-4 minutes, so a session queued at the cap waits roughly that
long for a replacement; the pilot queue wait is 600 s and the ready target 2
(about $0.32/hour; 3 ready is about $0.48/hour). Suite: 56 passed.

Open items before scaling up:

- **Queued sessions at the cap are not admitted until a replacement worker is
  ready** (about 4 minutes) and expire at the queue wait; there is no client-side
  retry. Size the ready target to expected burst rather than relying on the queue.
- **ARM delete time varies** (6-67 s), cause unknown, and each tick spawns SDK
  child processes; deriving health from one list call would reduce cost.
- **Policy generation is offline.** `az confcom` cannot run in Container Apps;
  an operator generates the policy base (`infra\pool\make_policy_base.py`) and
  the controller injects it and refuses mismatches.
- **Clients must trust or pin the internal environment's self-signed
  certificate.**

Remaining production gaps: a permanent probe path (the project VPN profile is
not usable from this machine), load and failure tests before growing toward
10 ready / 20 maximum, and review of the pilot's extra registry items.

See [cost assumptions and estimates](session-pool-costs.md) for pricing. This
document describes the selected architecture, not a deployment approval.

## Why one group per session

All workspaces, Python, shell, package-installation and test processes of a
shared MCP container run in the same container. Ownership checks authorize
normal MCP requests; they do not restrict what arbitrary code can access inside
that container. The earlier single-group demo worked that way and has been
removed (see [Removal decision](#removal-decision-2026-10-13)).

| Surface | Pooled design |
|---|---|
| Execution boundary | A separate confidential group per analysis session |
| Session data | Exists only in its assigned worker group |
| Agent endpoint | One private gateway MCP endpoint |
| Worker allocation | Atomically lease a ready group |
| Worker authentication | Gateway-to-worker mutual TLS |
| Ownership | Gateway checks authenticated ownership and persistent lease on every operation |
| Attestation | Verify the selected worker and bind evidence to its authenticated TLS channel |
| Retirement | Close the lease, then destroy the whole group |
| Scaling | Bounded ready pool, queuing, replenishment, and reconciliation |

Separate containers within a group can have filesystem/process separation, but
they still share the group lifecycle, local network, and confidential boundary.
The design does not rely on ordinary in-group container separation for
mutually untrusted analysis sessions.
## Components and trust boundaries

| Component | Responsibility | Trust and access |
|---|---|---|
| Agent/front-end | Authenticate the person and call MCP tools | Must not distribute service credentials to end users |
| MCP gateway | Authorize requests, acquire sessions, and forward tools | Trusted; sees routed inputs/results; cannot execute submitted code locally |
| Pool controller | Provision, verify, replenish, retire, and reconcile workers | Trusted Azure management identity, narrowly scoped to pool resources |
| Persistent lease store | Record ownership, worker state, expiry, and durable work | Private Table storage with Entra access and conditional transactions |
| Worker group | Execute one analysis session | Separate confidential boundary; no controller/gateway private credentials |
| Attestation sidecar | Obtain SNP/MAA evidence | Inside that worker's group; not a separate end-user sandbox |
| PKI | Issue gateway-client and worker-server certificates | Signing keys stay outside untrusted workers |

The gateway and controller are proposed as two ordinary Container Apps in one
internal VNet-connected environment, with separate identities. Their control
plane is not automatically confidential. If the requirement is to hide data
from gateway administrators as well, this architecture is insufficient and
requires a different end-to-end trust design.

The TEE protects worker memory from the infrastructure host. It does not protect
the worker from code intentionally executing inside it, guarantee correct
results from malicious code, or prevent data disclosure by an authorized caller.
Deployment, PKI, gateway, and key-policy administrators remain trusted.

## How an analysis session works

1. An agent calls `create_session` on the single gateway endpoint. The gateway
   derives ownership from validated authentication, not from a prompt or an
   arbitrary `X-User-Id` supplied by an untrusted client.
2. The gateway leases one ready worker using a conditional transaction. It
   returns an opaque session ID. If no verified worker is available, the request
   is queued with an explicit status; it is not reported as ready.
3. The gateway uses the recorded private worker address and verified mutual TLS.
   It never accepts a worker URL or Azure resource ID chosen by the model.
4. The controller/gateway verifies fresh worker attestation against the approved
   CCE policy and TLS identity before allowing user data into the worker.
5. File, Python, shell, install, git, and test tools operate in the same leased
   worker for the lifetime of that analysis. Other sessions are in other groups.
6. Release, idle expiry, absolute lifetime expiry, or a terminal failure closes
   the lease. New tool calls are rejected, in-flight work is bounded, and the
   controller destroys the group and its session-specific resources.
7. If capacity permits, the controller creates a fresh replacement. A used
   worker is never returned to the ready pool.

The agent can retain familiar tool names: `run_python`, `run_command`,
`upload_file`, `download_file`, `write_file`, `read_file`, `list_dir`,
`pip_install`, `run_tests`, and `git_clone`. The gateway also provides acquisition
status and owner-filtered listing. In pooled mode, attestation is associated
with a specific leased worker rather than one global shared server.

Trusted Agent Framework tool closures can bind the opaque session ID so the
model does not choose routing arguments. A separate closure alone is not the
execution boundary; the assigned confidential group supplies that boundary.

## Pool sizing and admission

The agreed initial settings are:

| Setting | Value |
|---|---:|
| Target concurrent active sessions | 20 |
| Desired ready workers | 10, when the cap permits |
| Maximum total workers | 20 |
| Proposed worker allocation | 3 vCPUs and 5 GB per group |

The worker allocation preserves the current shape: 2 vCPUs/4 GB for execution
and 1 vCPU/1 GB for the attestation sidecar. It is a starting allocation, not a
measured requirement for every spreadsheet workload.

The invariant is:

**active + ready + reserved/provisioning + draining/deleting <= 20**

The ready target is not 10 extra workers. At 20 active sessions, there is no
ready reserve. Failed or deleting groups temporarily reduce usable capacity.
A burst beyond ready capacity may wait for provisioning; at the total cap it
must queue or fail explicitly after a bounded wait.

Twenty pooled workers allocate 60 cores against the current verified 100-core
confidential ACI quota.
Quota availability does not reserve regional hardware or establish an SLA.
Recheck quota, capacity, budget, and lifecycle throughput before growing.

The proposed idle timeout is 30 minutes, absolute lifetime 4 hours, and queue
wait 5 minutes. These are proposed policy values requiring approval before
activation. Running work maintains activity but cannot extend the absolute
lifetime indefinitely.

## Networking: shared infrastructure, separate workers

Reuse the existing VNet, VPN connectivity, DNS resolver, NAT gateway, and ACR,
and their approved private connectivity. Do not create a VNet, VPN, NAT gateway, or
private endpoint for each analysis session.

Workers run in a dedicated delegated pool subnet (`snet-pool-workers`), sized for
the 20-worker target; the older `/28` `snet-aci` subnet (11 usable addresses) is
not used by the pool. The private control environment requires a different
subnet; it cannot occupy the ACI-delegated subnet.

Each group deployment references the existing pool subnet ID. Azure assigns a
private IP, and the controller records it. Worker certificates/private DNS are
managed automatically within an approved shared namespace. Clients only need
the gateway's stable DNS name and existing VPN/VNet access.

Network policy must:

- Allow gateway-to-worker traffic on required ports.
- Restrict worker-to-worker access and worker access to control/state services.
- Permit required DNS, attestation, and platform/bootstrap traffic.
- Restrict runtime egress; package/git access is explicitly approved rather than
  unlimited by default.

NAT provides outbound connectivity; it is not an egress allowlist. A shared
subnet does not automatically prevent workers from communicating.

## Authentication, TLS, and attestation

Use **Entra authentication and explicit permission checks at the gateway**.
Request ownership must come from validated claims or an explicitly trusted
front-end's authenticated user context.

Use **standard mutual TLS between the gateway and workers**:

- Each worker receives a unique server certificate/private key.
- Workers receive only the public gateway-client issuer trust chain.
- The gateway retains its client private key.
- Issuing CA private keys, controller identities, storage credentials, and
  agent secrets are never injected into execution workers.
- Enforce certificate verification, client authentication, certificate purpose,
  and hostname/worker identity. No `verify=False` or plaintext fallback.

Fresh attestation must cover the expected worker, CCE policy, applicable SNP
security claims, and a challenge bound to the TLS key. Use documented MAA
evidence and supported verification libraries. Fetching a token or checking its
unverified payload is not sufficient. Attestation is not a guarantee that
arbitrary executed code behaves correctly after startup.

### Why no HSM

The pool has no Managed HSM and no Secure Key Release (SKR):

- Workers attest to the controller through Microsoft Azure Attestation (MAA);
  the evidence is bound to the worker's TLS key, so the gateway only sends data
  to a worker whose attested key matches the channel.
- Per-worker keys derive from the controller-held CA; the controller issues each
  worker's certificate.
- No secret is released to workers, so there is nothing for an HSM to gate.
- If a secret must later be released to attested workers, a Key Vault Premium
  SKR path can be added, scoped per session/worker. Never expose a fleet-wide
  released secret to untrusted code.

The `mcr.microsoft.com/aci/skr` sidecar image is still used, but only to produce
attestation reports (`/attest/maa`); its key-release API is not called. The
`SKR_MAA_ENDPOINT` setting name is historical and holds the MAA hostname.
## Reliability and lifecycle

Use persistent Table state, ETags, and same-partition transactions for allocation.
Process-local locks alone do not prevent two gateway instances leasing the same
worker. Reserve capacity before provisioning and retain the reservation until
deletion is confirmed.

The controller records Azure operation IDs and uses idempotent, bounded retries.
A reconciliation loop compares tagged pool groups with persistent state and
recovers orphaned, missing, failed, and stuck resources. It must not delete
unrelated resources.

Before every dispatch, check owner, lease state/generation, expiry, and permission.
Fail closed if authoritative state is unavailable. Do not automatically replay
arbitrary code when a disconnected request has an unknown execution outcome.

Keep customer data/code, credentials, and private keys out of control records
and diagnostic logs. Bound uploads, results, queue length, operation time, and
session lifetime.

## Implementation and rollout

| Stage | Change | Required gate |
|---|---|---|
| Feasibility code | Explicit single-session worker mode, required mTLS, retirement, fresh TLS-bound attestation, and two-worker preparation/probes | Local protocol/auth/state tests pass; no live-isolation claim |
| Live two-worker spike | Two fresh confidential groups on approved networking | Verify TLS/attestation before data; establish separation, denied cross-access, and complete destruction |
| Controller/state | Atomic leasing, durable work, replenishment, limits, and reconciliation | Race/crash/retry tests cannot double-lease, leak capacity, or recycle used workers |
| MCP gateway | Single endpoint, Entra authorization, complete tool routing, acquisition/status, and bounded errors | Cross-owner, expired, released, and unready sessions cannot dispatch |
| Pilot | Expand from two workers to 10 ready/20 maximum | Representative load, burst/queue, memory, expiry, failure, and cost measurements |
| Production/growth | Monitoring, runbooks, budget limits, and controlled expansion | Explicit production approval; renewed quota/network/capacity review |

Implementation approval does not authorize Azure deployment or cost-incurring
tests.

### Local control-plane entrypoints

From the repository root:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_pool_feasibility.py tests\test_pool_control.py
.\.venv\Scripts\python.exe pool-control\service.py --help
.\.venv\Scripts\python.exe pool-control\service.py gateway
.\.venv\Scripts\python.exe pool-control\service.py controller
```

`--help` lists required environment settings. Lifetimes, queue/operation and
data limits must be explicitly configured; the proposed values are not defaults.
The controller requires `POOL_MATERIAL_FACTORY=importable_module:function`
implementing the durable material contract in `pool-control\azure_adapter.py`.
Missing configuration/material fails closed; there is no production mock
fallback. These service commands require configured infrastructure and do not
deploy the hosting/network prerequisites.

## Alternatives and references

[ACA Sandboxes](https://learn.microsoft.com/azure/container-apps/sandboxes-overview)
is a separate product from dynamic sessions. It documents per-sandbox isolation,
prewarming, suspend/resume, storage, networking controls, and programmable
lifecycle. Those are useful capabilities, but the reviewed documentation does
not establish confidential-computing support for `Microsoft.App/SandboxGroups`.

[Confidential compute workload profiles in Container Apps](https://learn.microsoft.com/azure/container-apps/confidential-compute)
apply to ordinary container apps. Their existence does not establish that ACA
Sandboxes inherit a DC-series confidential profile.

- [Confidential ACI overview](https://learn.microsoft.com/azure/container-instances/container-instances-confidential-overview)
- [ACI container groups](https://learn.microsoft.com/azure/container-instances/container-instances-container-groups)
- [ACI VNet networking](https://learn.microsoft.com/azure/container-instances/container-instances-virtual-network-concepts)
- [ACI limits and quotas](https://learn.microsoft.com/azure/container-instances/container-instances-resource-and-quota-limits)
- [Microsoft confidential sidecar API](https://github.com/microsoft/confidential-sidecar-containers/blob/main/cmd/skr/README.md)

## Removal decision (2026-10-13)

The Azure Managed HSM, Secure Key Release (SKR), and the legacy single-sandbox
design were removed from the architecture on 2026-10-13. The pooled design does
not use them (see [Why no HSM](#why-no-hsm)), and the HSM was the largest fixed
cost (about $2,336/month; see [costs](session-pool-costs.md)).

Azure resources removed by the owner: the legacy container group
`mcp-build-sandbox` (resource group `rg-confidential-mcp`) and the Managed HSM
`hsm-cmcp-visdzn` (deleted and purged). Repository changes: legacy infra
(`deploy.ps1`, `deploy-vnet.ps1`, parameter/template examples, SKR release policy,
`require-tls-policy.ps1`), the legacy notebooks, and the SKR/bearer/Entra
single-session code path in `mcp-build-sandbox\server.py` were deleted.

Remaining and shared: the VNet, `snet-aci` (the legacy group's subnet; it still
exists but nothing in this repo uses it, and it is not claimed removed), the NAT
gateway `natgw-aci` / `pip-aci-nat` (shared with the pool subnet), VPN, DNS
resolver, and ACR `acrconfmcpvisdzn`. The pool uses its own subnet,
`snet-pool-workers`. Key Vault `kv-cmcp-visdzn` is out of scope; the project does
not require a Key Vault or HSM.

**Next release note:** the cleaned `mcp-build-sandbox` source (server.py,
requirements.txt) has not been rebuilt. The deployed worker image digest still
contains the old code, which is inert in pooled mode. The next release must
rebuild and push the worker image, then regenerate the policy base
(`infra\pool\make_policy_base.py` / `finalize_policy.py`) against the new digest.
