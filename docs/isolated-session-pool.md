# Confidential analysis session pool: design and rollout

## Decision and current status

Use **one confidential ACI group per analysis session**, behind one private
Entra-authenticated MCP gateway. Each group is a separate Hyper-V-isolated
AMD SEV-SNP TEE with a worker and attestation sidecar. A controller implements
prewarming, exclusive leasing, provisioning, admission and destruction.

The worker, gateway/controller, Table state, infrastructure and client paths are
implemented, including a per-session encrypted channel (X25519 key agreement,
AES-256-GCM) bound to the attested worker. Live validation so far covers a
3-maximum/2-ready configuration, not the 10-ready/20-maximum scale target.
Current Azure state is recorded in [operations](operations.md), not here.

See [architecture](ARCHITECTURE.md), [operations/live gate](operations.md),
[requirements assessment](requirements-assessment.md) and
[costs](session-pool-costs.md).

## Why a group per session

Directory permissions, Python virtual environments, or multiple processes in
one shared interpreter are not a boundary for mutually untrusted arbitrary code.
A session gets a fresh group for its entire lifetime; after release or expiry
the group is destroyed, not cleaned and recycled.

| Surface | Design invariant |
|---|---|
| Confidential boundary | Entire ACI group, one analysis session |
| Gateway routing | Opaque lease, authorized owner, private worker target |
| Allocation | Persistent exclusive lease with ETags/same-partition transactions |
| Admission | Approved policy/security baseline and fresh TLS-key-bound attestation |
| Execution | Gateway-to-worker mTLS; arbitrary code has worker permissions |
| Retirement | Reject further dispatch, then destroy whole group |
| Capacity | All ready/provisioning/active/draining/deleting groups count |

Separate groups isolate execution, but network isolation also matters: a
dedicated worker subnet/NSG denies worker-to-worker and worker-to-control/state
traffic. Control-to-worker TLS traffic is permitted. Restricted outbound Azure
services support image pulls and attestation; there is no general-purpose
package/Git egress.

## Authentication, ownership and on-premises calling

Callers reach the private gateway through approved routing and DNS. Entra
authentication and pinned HTTPS are independent of that route. The API
requires the correct audience and `Sandbox.Access`; tenant, issuer, signature,
validity and permission claims are validated.

The gateway takes ownership from the authenticated principal, not from a
model-supplied user/session route. All calls check the persistent lease.
Sharing an app identity shares ownership; this implementation does not supply
per-human OBO/delegated authorization for a multi-user front-end.

The sample Agent Framework loop runs on-premises. A separate Foundry identity
provides inference. Trusted local code owns allocation, upload/download and
cleanup; typed tool closures bind execution to the allocated session. Tokens,
credentials and lease IDs are not exposed as model tool parameters.

## Trust boundaries and residual risks

The TEE protects worker memory from the infrastructure host, not from code
inside the worker or authorized recipients of results. On-premises code,
Foundry inference, gateway/controller and PKI administrators remain outside the
confidential boundary. Gateway administrators can see routed data; model-visible
tool summaries leave the TEE. This is not confidential end-to-end inference.

These risk acceptances are **not defaults for another deployment**:

- **SMT-enabled hosts:** side channels between threads sharing a core are not
  removed by SEV-SNP memory encryption. Admission must reflect an explicitly
  approved risk decision.
- **Registry pull identity:** private image pulls require an attached pull-only
  identity. Generated code runs in an empty network namespace and cannot reach the
  instance metadata endpoint that would issue its token, but the identity still exists
  on the worker. Limit AcrPull to the intended registry and explicitly approve this
  residual exposure.
- **Security versions:** minimum SNP/UVM versions must be an operator-approved
  baseline, not values copied blindly from a historical host.

## TLS, attestation and policy

Each worker generates its own TLS key inside the TEE at startup; the private key
lives only in memory-backed worker storage and is never in configuration, the
environment or controller storage. The controller issues no worker certificate.
The gateway authenticates to the worker with its client certificate (mTLS) and
trusts the worker only after MAA evidence commits to the TLS public key it
observed on that same connection; later calls are pinned to that key. A
man-in-the-middle key therefore fails admission. Gateway-to-worker traffic is
never plaintext.

On top of TLS, admission establishes a per-session key (X25519 + AES-256-GCM)
whose public half is also committed in the attestation, so request and result
payloads stay encrypted to the attested worker.

Admission verifies MAA signatures/issuer, fresh challenge, TLS public-key and
session-key binding, approved CCE policy hash and applicable security claims/SVN minimums.
Retrieving or decoding `get_attestation` output is **not verification** and does
not attest the outside-TEE gateway or future arbitrary code behavior.

Policy generation is offline (`az confcom` plus Docker). The controller uses an
approved base and rejects mismatches. Worker source changes require image
rebuild/push, new pinned digest, regenerated policy base and reviewed release.

## Why no HSM

The pool has no HSM or Secure Key Release path. Workers attest through MAA;
there is no application secret whose release must be gated by attestation.
The gateway client CA remains trusted control-plane material. The
`mcr.microsoft.com/aci/skr` sidecar produces evidence only; its key-release API is
not used. An SKR path would be a separately reviewed future feature.

## Reliability and bounded lifecycle

Table-backed leases use ETags and same-partition transactions rather than
process-local locks. Capacity is reserved before provisioning and held until
deletion is confirmed. The controller reconciles tagged pool inventory, missing,
failed and stuck workers with durable state.

Every dispatch checks owner, lease/generation, expiry and operation bounds.
Unknown execution outcomes are not replayed; the whole session is retired.
Results arriving after expiry/release are discarded. Upload/result/queue/time
bounds limit resource use, but serialized/base64 overhead matters.

Historical settings were 30-minute idle, 4-hour absolute lifetime, 300-second
operations and 600-second queue wait. A cold replacement took about four minutes
and deletes varied substantially. At capacity, queued sessions wait for confirmed
retirement and a ready replacement; they may expire. Timings are not SLAs.

## Lease table and session queue

All pool state is one Table partition, changed only by ETag-checked
transactions. Rows hold metadata only, never user data or keys.

| Row | Purpose | Key fields |
|---|---|---|
| `pool` | Optimistic-concurrency root | `revision` |
| `control` | Administrative switches | `admission_paused` |
| `s:<id>` | One session lease | `owner`, `state`, `generation`, `created`, `queue_until`, `worker` |
| `w:<id>` | One worker group | `state`, `lifetime`, `session`, `generation`, `claim`, `claim_until`, `delete_after` |

Session states: `queued` -> `activating` -> `active` -> `closed`, or
`expired` / `failed`. Worker states: `reserved` -> `provisioning` -> `ready` ->
`activating` -> `active` -> `draining` -> `deleting`, or `quarantined`.

1. **Create.** `create_session` adds a `queued` row with `queue_until`
   (now + queue wait). It is refused if admission is paused or the queue is full.
2. **Reserve.** Each controller tick adds `reserved` worker rows while total
   workers are below the cap and unassigned workers are below the desired-ready
   count. Capacity is held until deletion is confirmed.
3. **Provision and verify.** A controller claims the worker (`claim_until` makes
   the claim time-limited), creates the group, verifies MAA evidence, and marks
   it `ready`.
4. **Assign.** The oldest unexpired `queued` session is paired with a `ready`
   worker in one transaction; both become `activating`, then `active` once the
   gateway confirms the worker accepted the session key.
5. **Retire.** Release, idle/absolute expiry or a failure bumps the session
   `generation` and marks the worker `draining`. The worker is deleted, never
   reused.
6. **Sweep.** A queued session past `queue_until` becomes `expired`. Stale
   claims are released and stuck workers are quarantined. Clients see the
   state through `get_session_status`; a full queue is rejected at create time.

Because every dispatch re-checks `generation`, a stale or retired lease cannot
reach a worker that has been reassigned.

Client cleanup cannot guarantee deletion after a killed process or broken
network. Service TTL and operator reconciliation are required backstops.

## Validation history and removal decision

The earlier two-worker probe checked fresh policy-bound confidential groups,
file/session separation, blocked cross-worker/control access, retirement and
destruction. The gateway pilot later checked create/upload/pandas/download/delete
and cross-owner denial. Initial burst/queue admission did not fully pass;
follow-up changes increased deletion timeouts, controller concurrency and queue
wait.

The most recent live run (2026-10-24, 3-maximum/2-ready, on the current worker,
sidecar and controller images and the attested-TLS channel) used the `smoke.py`
phases as an in-VNet job with two caller identities:

- `basic`: create, activate, upload, pandas run, download, delete, cross-owner
  status/exec/delete rejected and an empty cross-owner list.
- `hostile`: non-root user, no secrets in the environment, server code and process
  environment unreadable, internet and instance metadata service blocked (generated
  code runs in an empty network namespace), and process, memory and file-size bombs
  contained.
- `storage`: the workspace is on the container overlay filesystem with no block-device
  or Azure Files mounts, and a marker written in one session was absent from a session
  on a new group.
- `fault`: deleting a worker group, and restarting the gateway, each retired the
  affected session ("outcome uncertain, not replayed"), later calls were refused, and
  a fresh session then activated on a new attested worker.

These runs do not establish representative load, burst or scale readiness, and
they do not exercise real on-premises/VPN connectivity.

The earlier shared single-group demo, Managed HSM and SKR path were removed.
Shared network/registry prerequisites remain roles in the design; this document
does not assert a current inventory. Always verify or rebuild the actual release
image rather than treating an old digest as current.

## Rollout gates

| Stage | Gate |
|---|---|
| Offline examples | Schema/import/control-flow tests with explicit fake model/MCP services; known synthetic CSV values |
| Approved live pilot | Real on-premises connectivity, independent permissions, notebook/tool execution, artifact checks and destruction |
| Scale toward 10/20 | Representative bursts, queue expiry, failed provisioning/deletion, restart/reconciliation and cost measurements |
| Production | Security/risk acceptance, user authorization model, monitoring, key/cert rotation, budget/runbooks and explicit approval |

Offline test success does not by itself authorize Azure deployment or
warm-up. Public cleanup removes environment values from the
current tracked tree, not Git history; history remediation requires separate
approval.

## Alternatives and references

[Dynamic sessions](https://learn.microsoft.com/azure/container-apps/sessions)
and [ACA Sandboxes](https://learn.microsoft.com/azure/container-apps/sandboxes-overview)
are separate managed execution products. Do not infer confidential SandboxGroups
support from confidential workload profiles for ordinary Container Apps.
Choose them when managed untrusted-code isolation is enough; choose this ACI
design when confidential memory/attestation is required.

- [Confidential ACI overview](https://learn.microsoft.com/azure/container-instances/container-instances-confidential-overview)
- [ACI networking](https://learn.microsoft.com/azure/container-instances/container-instances-virtual-network-concepts)
- [ACI quotas](https://learn.microsoft.com/azure/container-instances/container-instances-resource-and-quota-limits)
- [Microsoft Azure Attestation](https://learn.microsoft.com/azure/attestation/overview)
- [Agent Framework samples](https://github.com/microsoft/agent-framework/tree/main/python/samples)
