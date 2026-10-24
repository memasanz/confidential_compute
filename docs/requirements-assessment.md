# How each requirement is satisfied

This document walks through the 25 requirements in
[`system-requirements.md`](../system-requirements.md) and shows, for each one,
**what it means in plain words, how this system satisfies it, what proves it,
and what is left over**. Where the requirements describe an outcome, this
document names the mechanism that produces it.

The approach differs from a sandbox-platform design. Here each session gets its
own **AMD SEV-SNP confidential container group** (Azure Container Instances,
confidential SKU), and the gateway checks **hardware-signed attestation** before
it releases any data. Hardware evidence, not only access control, is what keeps
the host and other tenants out.

## Contents

1. [The idea in one paragraph](#the-idea-in-one-paragraph)
2. [Trust model](#trust-model)
3. [The controls that do the work](#the-controls-that-do-the-work)
4. [Life of one session](#life-of-one-session)
5. [Status at a glance](#status-at-a-glance)
6. [Requirement-by-requirement](#requirement-by-requirement)
7. [Evidence: tests and live checks](#evidence-tests-and-live-checks)
8. [Open items for sign-off](#open-items-for-sign-off)

## The idea in one paragraph

For every analysis session, a controller creates a brand-new confidential
container group (the **worker**). The worker's memory is encrypted by the CPU, so
the Azure host cannot read it. Before sending anything, the gateway asks the
worker for a hardware-signed report and checks it with Microsoft Azure
Attestation (MAA): is this the approved image, the approved policy, and a current
security level? Only then does the gateway create a random 256-bit key, lock it so
that only that attested worker can open it, and use it to encrypt every request
and reply for the session. When the session ends, or anything goes wrong, the
worker is destroyed and the key is forgotten. A used worker is never reused.

Three words used below:

- **Worker:** one throwaway confidential container group, for one session.
- **Gateway:** the private MCP service that authenticates callers and talks to
  workers. It is the only party that holds the session key besides the worker.
- **Controller:** the service that creates, checks and destroys workers.

## Trust model

This design accepts the **narrower administrative boundary** from the
requirements' open decision: workload administrators (gateway, controller, PKI)
are constrained by separately owned Azure Policy and RBAC, and an independent
security authority controls those policies.

| Who or what | Trusted with plaintext? | Why |
|---|---|---|
| The Azure host and hypervisor | **No** | Worker memory is encrypted by the CPU (SEV-SNP) and the policy pins what runs |
| Other sessions and other workers | **No** | One group per session; the worker subnet denies worker-to-worker traffic |
| The network between components | **No** | Mutual TLS plus a separate per-session encrypted channel |
| The on-premises caller | **Yes** | It owns the data and authenticates with Entra |
| The gateway and its administrators | **Yes (accepted)** | Files and results pass through the gateway in plaintext by design (see below) |
| A separate security authority | **Yes** | Owns the policies and permissions that keep administrators out of the worker. It must be a different group from the workload administrators |
| Microsoft platform operators | **Yes** | Normal platform controls; hardware attestation narrows this but does not remove it |
| The Foundry model | Only for what the caller sends it | Tool results returned to the model leave the confidential boundary |

Two things this design deliberately does **not** claim: protection against
*every* Azure administrator regardless of policy ownership, and a gateway that
never sees plaintext. Both are in the README's
[administrator trust model](../README.md#administrator-trust-model).

### What "plaintext through the gateway" means

The gateway handles your data unencrypted in its own memory. That covers the
files you upload, the code you submit, and the results and downloads that come back.

- A caller reaches the gateway over HTTPS, and the gateway ends that TLS, so it
  sees the content. It needs to, to check ownership and route each call.
- It then puts the content into the per-session AES-256-GCM envelope and sends it to
  the worker over mutual TLS. Results return the same way: the gateway decrypts
  them, then re-encrypts them over HTTPS to the caller.
- Plaintext therefore exists in three places only: the calling application, the
  gateway (in memory), and the worker inside the TEE. The controller, lease store,
  logs and registry never receive file contents or code.
- The gateway is outside the TEE. Anyone who can run code on it or capture its
  memory could read data in transit, which is why its administrators must be
  trusted and kept under the separately owned policies above.
- Removing this would need encryption from the client to the worker with the
  gateway only relaying ciphertext. The gateway could then no longer inspect
  content, and this design does not do that.

## The controls that do the work

Most requirements are met by combining a small set of controls.

| Control | What it is | Requirements it supports |
|---|---|---|
| **Confidential container group** | SEV-SNP memory encryption, `restartPolicy: Never`, private IP only | 1, 3, 5, 8, 13, 14, 21 |
| **CCE policy, digest-pinned** | A generated policy fixes the exact images, commands, environment, mounts and resources; its hash is what the hardware signs | 3, 4, 24 |
| **MAA attestation, fresh and bound** | The worker's report must carry a fresh nonce, the policy hash, a minimum security version, its TLS key fingerprint and its session public key | 3, 4, 9, 10 |
| **Per-session encrypted channel** | X25519 key agreement to the attested key, then AES-256-GCM envelopes with a counter | 9, 10, 11, 12, 22 |
| **Entra authentication and ownership** | `Sandbox.Access` role; every session belongs to the validated caller | 6, 10, 13 |
| **Controller lifecycle and cleaner** | The controller destroys workers; an independent job deletes old ones by tag | 5, 14, 15, 16 |
| **Worker hardening** | Unprivileged user, scrubbed environment, resource limits, locked-down subnet | 18, 19, 20, 21 |
| **Content-free logging** | No per-request lines, no tool error text | 23 |

## Life of one session

1. The caller gets an Entra token and calls `create_session`. The gateway records
   a lease owned by that caller (req 6, 13).
2. The controller assigns a ready worker, or creates one from the approved image
   and policy (req 24). It checks the group's template against the approved shape
   before admitting it (req 3).
3. The gateway asks the worker for evidence with a fresh nonce and verifies it
   with MAA: signature, nonce, policy hash, security version, TLS fingerprint and
   the worker's session public key (req 4).
4. The gateway generates a random 256-bit key, wraps it to the attested public key
   and sends it once. The worker accepts one key only (req 11, 12).
5. Every file and execution call travels as an AES-256-GCM envelope. Generated
   code runs as an unprivileged user inside the worker (req 9, 10, 17 to 22).
6. On release, failure, idle timeout or absolute timeout, the controller destroys
   the group and the gateway drops the key. A retry gets a new group and a new key
   (req 14, 15). A scheduled job removes any group that outlives its maximum age
   (req 16).

## Status at a glance

**Met** means implemented and covered by tests. **Live** means it was also
exercised against the deployed pool (`infra/pool/smoke.py`, phases `basic`,
`hostile`, `storage` and `fault`, 2026-10-24). **Partial** items carry a stated
residual risk.

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 1 | Administrator confidentiality | Met (accepted boundary) | Tests, policy |
| 2 | Explicit plaintext boundary | Met (accepted boundary) | Design, tests |
| 3 | Tamper resistance | Met | Tests |
| 4 | Verified execution identity | Met | Tests, live |
| 5 | Accept availability attacks | Met | Tests |
| 6 | User authorization | Met | Tests, live |
| 7 | Least-privilege identity | Met | Infra, template |
| 8 | Restricted access | Met | Infra, live |
| 9 | Protected communications | Met | Tests, live |
| 10 | Session-specific authorization | Met | Tests, live |
| 11 | Unique ephemeral secrets | Met | Tests |
| 12 | Restricted secret residency | Met | Tests, live (attested TLS) |
| 13 | Session isolation | Met | Tests, live |
| 14 | No recovery or resumption | Met | Tests |
| 15 | Deterministic termination | Met | Tests, live |
| 16 | Independent orphan cleanup | Met | Tests, live |
| 17 | Interpreter functionality | Met | Tests, live |
| 18 | Separate untrusted code | Met | Tests, live |
| 19 | Resource limits | Met | Tests, live |
| 20 | Restricted outbound access | Met | Tests, live |
| 21 | Protected temporary data | Met (platform deletion claim accepted) | Tests, live |
| 22 | Direct result delivery | Met (accepted boundary) | Tests, live |
| 23 | Content-free telemetry | Met | Code review, tests |
| 24 | Approved, immutable deployment | Met | Pinned; release record signed |
| 25 | Demonstrable enforcement | Met | Tests, live fault injection |

## Requirement-by-requirement

Each entry has the same parts: **Meaning**, **How it is satisfied**, **Evidence**
and **Residual**.

### Confidentiality and trust

#### 1. Administrator confidentiality

**Meaning.** Holding Azure administrative rights must not be enough to read a
session's files, code, results or secrets, even while it runs.

**How it is satisfied.**
- The worker is a confidential container group. The CPU encrypts its memory, so a
  host or hypervisor administrator cannot read it.
- The CCE policy fixes what may run. It blocks the usual administrator routes
  into a container: no exec shell, no attached volumes, and a locked-down
  environment and command set.
- The session key exists only in gateway and worker memory. It is never in a
  portal setting, secret or storage account, so there is nothing to list or export.
- Workload administrators are constrained by separately owned Azure Policy and RBAC
  (the accepted narrower boundary).

**Evidence.** `test_base_policy_injection_binds_images_and_shape`,
`test_worker_content_tools_require_the_session_key`; live `basic` and `hostile`
runs.

**Residual.** Whoever can change the governing policies and permissions could
weaken the setup. That is handled by separation of duties, not by cryptography. The
gateway sees plaintext (req 2).

#### 2. Explicit plaintext boundary

**Meaning.** Only the trusted calling application and the approved execution
environment may handle sensitive plaintext.

**How it is satisfied.** Plaintext exists in three places: the calling
application, the gateway, and the worker. Between the gateway and the worker it is
inside the per-session encrypted channel, inside mutual TLS. The controller, lease
store, logs and registry never receive file contents or code. Tool results sent to
a model leave the boundary by the caller's choice; the docs say so plainly.

**Evidence.** `test_all_tools_routed_results_and_errors_unchanged`,
`test_real_gateway_entra_mcp_worker_mtls_with_fake_azure_and_maa`.

**Residual.** The gateway is part of the plaintext boundary and its administrators
are trusted (accepted).

#### 3. Tamper resistance

**Meaning.** If an administrator changes the execution environment, either
confidentiality still holds or the application refuses to use it.

**How it is satisfied.**
- *Prevent:* the CCE policy pins images by digest, plus commands, environment
  variable rules, mounts and resources. The container group will not start with
  anything else.
- *Detect:* before admission the controller reads the group back and compares its
  template with the approved shape (images, resources, environment names, TLS
  certificate names). Any extra or changed setting fails the worker.
- *Verify:* the gateway checks hardware evidence. A modified policy changes the
  signed hash, so the evidence no longer matches.

**Evidence.** `test_base_policy_injection_binds_images_and_shape`,
`test_substitute_worker_evidence_is_rejected`,
`test_pinned_worker_transport_refuses_unverified_tls_and_changed_origins`,
`test_tls_configuration_fails_closed`.

**Residual.** None beyond the trust model.

#### 4. Verified execution identity

**Meaning.** Before releasing anything, independently establish that the endpoint
is the approved environment, using fresh, replay-resistant evidence bound to this
session's cryptographic identity.

**How it is satisfied.** The gateway sends a fresh random nonce. The worker builds
a challenge containing the nonce, its id, the session id, the SHA-256 of its TLS
public key and its TEE-generated session public key, and asks the SEV-SNP hardware
(via the attestation sidecar) to sign a report over it. The gateway verifies the
MAA token's signature, issuer, expiry, the policy hash, the minimum security
version and that the challenge matches. The hardware-signed report therefore
proves the approved code is running on genuine hardware and holds *that* TLS key
and *that* session key.

**Evidence.** `test_attestation_challenge_and_errors`,
`test_signed_claim_verifier_is_not_live_attestation`,
`test_substitute_worker_evidence_is_rejected`; live admission of real workers.

**Residual.** Trust roots are the AMD hardware and MAA. Everything else is checked
in code.

#### 5. Accept availability attacks

**Meaning.** An administrator may stop or delete sessions. That must cause failure,
not disclosure.

**How it is satisfied.** Every dispatch checks the lease and the worker's state.
A stopped, deleted, unreachable or mismatched worker produces an error and retires
the session. There is no fallback path to another, less protected, environment.
An uncertain outcome is never replayed.

**Evidence.** `test_activation_crash_is_never_replayed_and_claim_fencing`,
`test_gateway_uncertain_outcome_no_replay_and_release_race`,
`test_idle_absolute_expiry_and_inflight_crash_retire`.

**Residual.** Availability is not protected, by design.

### Identity, authorization and communication

#### 6. User authorization

**Meaning.** Authenticate and authorize the user before creating or reading a
session.

**How it is satisfied.** The gateway validates each Entra token: tenant, issuer,
signature, audience, validity and the `Sandbox.Access` role. The session owner is
taken from the token's object id (users) or application id (services), never from
a request field. Every tool call re-checks that the caller owns the lease.

**Evidence.** `test_entra_signatures_audience_owner_and_permission`; live
cross-owner status, exec and delete were all rejected.

**Residual.** A shared service principal means a shared owner. The sample does not
implement per-person delegated authorization.

#### 7. Least-privilege application identity

**Meaning.** Give the application only the Azure permissions it needs.

**How it is satisfied.** The gateway and controller run with separate managed
identities. The controller can manage container groups and DNS records in its own
resource groups, and reach the lease table; the gateway has no Azure management
rights. The worker's only identity is a registry pull role on one registry. The
orphan cleaner holds only the rights to delete worker groups.

**Evidence.** `infra/pool/deploy-pool-infra.ps1`, `infra/pool/bicep/`, and the
role assignments documented in the [operations guide](operations.md).

**Residual.** Broad roles inherited from the subscription sit outside the
workload and are covered by the Azure Policy decision in the trust model.

#### 8. Restricted access

**Meaning.** Not exposed to the public internet; inbound access limited to
approved systems.

**How it is satisfied.**
- Workers have private IP addresses only, in a delegated subnet. The subnet's
  network security group allows TCP 8000 only from the control subnet and denies
  everything else inbound, including worker-to-worker traffic.
- The gateway lives in an internal Container Apps environment, reachable only
  through private routing and private DNS. The lease store has public network
  access disabled and shared-key access off.
- Callers must present an Entra token and pin the gateway certificate.

**Evidence.** `infra/pool/bicep/nsg.bicep`, `state.bicep`; live smoke ran only over
the private path.

**Residual.** None.

#### 9. Protected communications

**Meaning.** Encrypt and authenticate sensitive traffic end to end; network
location alone must not create trust.

**How it is satisfied.** Two layers. First, mutual TLS between gateway and worker:
the worker presents a certificate issued for its own hostname, the gateway presents
a client certificate, and the gateway pins the attested TLS key fingerprint. Second,
the per-session encrypted channel: every content request and reply is an
AES-256-GCM envelope under the session key, so even someone who held the TLS key
could not read or forge session content.

**Evidence.** `test_real_tls_client_purposes`, `test_real_uvicorn_mtls_mcp_lifecycle`,
`test_session_key_wrap_and_encrypted_channel`; live `basic`.

**Residual.** The channel ends at the gateway, which holds plaintext (accepted).

#### 10. Session-specific authorization

**Meaning.** Every sensitive operation needs session-specific authentication. Reaching
the network or holding Azure permissions must not be enough.

**How it is satisfied.** On the worker, the file and execution tools are not plain
MCP tools. Only `create_session`, `list_sessions`, `delete_session`, `get_status`
and `get_attestation` are plain. Everything else is reachable only through `invoke`,
which opens an authenticated envelope. Envelopes use directional subkeys and a
strictly increasing counter, so replayed, reordered, modified, wrong-direction and
wrong-key messages are rejected.

**Evidence.** `test_worker_content_tools_require_the_session_key`,
`test_session_key_wrap_and_encrypted_channel`.

**Residual.** None.

### Session secrets and lifecycle

#### 11. Unique ephemeral secrets

**Meaning.** A new cryptographically random secret for every session (256-bit).

**How it is satisfied.** The gateway generates a random 256-bit session key per
session with the operating system's random source. Directional subkeys are derived
from it with HKDF-SHA256 bound to the worker and session. The worker accepts exactly
one key; a second attempt is refused.

**Evidence.** `test_session_key_wrap_and_encrypted_channel`,
`test_session_key_established_once_and_dropped_on_release`.

**Residual.** None.

#### 12. Restricted secret residency

**Meaning.** Usable session secrets exist only in trusted application memory and the
approved environment, never in configuration, environment variables, databases,
storage or logs.

**How it is satisfied.** The session key is created in gateway memory, wrapped to the
worker's attested public key (X25519 ECDH, HKDF, AES-GCM) and sent once. It is never
written to disk, a table, a setting or a log. The worker's private half never leaves
the TEE. Deleting the session, or restarting the gateway, destroys it.

**Evidence.** `test_session_key_established_once_and_dropped_on_release`,
`test_gateway_restart_cannot_resume_a_keyed_session`.

**Residual.** None. The worker generates its own TLS key inside the TEE (*attested
TLS*); the gateway trusts that key only after MAA evidence binds it, so no TLS private
key or issuing CA key is injected into a worker. The gateway client CA is trusted
control-plane material and cannot read or forge session content.

#### 13. Session isolation

**Meaning.** Each session has its own execution environment; sessions cannot reach
each other's data or credentials.

**How it is satisfied.** One confidential group per session, never reused. Each worker
has its own TLS material and session key. The subnet denies worker-to-worker traffic.
The lease table ties each worker to one owner.

**Evidence.** `test_atomic_concurrent_reservation_assignment_and_dispatch`,
`test_owner_expiry_queue_bounds_and_stale_generation`; live: cross-owner status, exec
and delete rejected, an empty cross-owner listing, and another session's files were
unreachable.

**Residual.** None.

#### 14. No recovery or resumption

**Meaning.** Restart, failed verification or a significant failure must invalidate the
session. A retry gets a new environment and new secrets.

**How it is satisfied.** Workers run with `restartPolicy: Never`. A worker that finds
its state file already used refuses to start a second lifetime. The session key is held
only in gateway memory and the worker accepts only one, so a gateway restart cannot
resume a keyed session; it is retired. A failed verification or lost connection retires
the worker, and `create_session` provisions a fresh one.

**Evidence.** `test_worker_never_restarts`, `test_retirement_persists_and_listing_attestation_block`,
`test_gateway_restart_cannot_resume_a_keyed_session`.

**Residual.** None. The gateway therefore runs as a single replica.

#### 15. Deterministic termination

**Meaning.** Destroy the environment and discard its secrets on completion or failure.

**How it is satisfied.** Releasing a session, an idle or absolute timeout, a failed
check or a crash all lead the controller to delete the container group and its DNS
record, and the gateway drops the channel key. A worker is never recycled.

**Evidence.** `test_controller_create_crash_missing_worker_uncertain_delete_and_replenish`,
`test_session_key_established_once_and_dropped_on_release`; live post-delete calls were
rejected and the pool returned to its target.

**Residual.** None.

#### 16. Independent orphan cleanup

**Meaning.** A short maximum lifetime, and removal of abandoned environments using only
resource metadata.

**How it is satisfied.** `infra/pool/orphan_cleanup.py`, run as a scheduled Container
Apps job with its own identity, lists worker groups, keeps only those tagged for this
pool, and deletes any that started more than a maximum age ago. It uses tags and start
times only, with no secrets, no connection to the worker and no contents. The pool also
enforces idle and absolute lifetimes itself.

**Evidence.** `test_orphan_cleanup_selects_only_old_groups_of_this_pool`; the job is
deployed and running.

**Residual.** None.

### Code execution and data handling

#### 17. Interpreter functionality

**Meaning.** Upload, list, retrieve and delete files; run Python; report status,
stdout/stderr and generated files; health checks.

**How it is satisfied.** Tools: `write_file`, `read_file`, `delete_file`, `list_dir`,
`upload_file`, `download_file`, `run_python`, `run_command`, `run_tests`, plus
`get_session_status`, `get_status` and `get_attestation`. `run_python` returns exit
code, stdout and stderr; generated files are found with `list_dir`.

**Evidence.** `test_all_tools_guard_lifetime`, `test_delete_file_and_status`; live
upload, pandas run and download of a verified CSV (totals 640 and 64).

**Residual.** None.

#### 18. Separate untrusted code

**Meaning.** AI-generated Python is isolated from the interpreter service, its memory,
keys and credentials.

**How it is satisfied.** The server drops to an unprivileged user (UID 10001) before
running generated code, with an empty environment (no TLS key, token or worker
setting), its own home and a new session id. The server's code and its own environment
are unreadable to that user.

**Evidence.** `test_generated_code_cannot_see_worker_secrets`; live `hostile`: non-root,
no secret environment names, server code and its environment blocked.

**Residual.** Hardening, not a hostile-code guarantee: a kernel escape would still be
inside the same confidential group.

#### 19. Resource limits

**Meaning.** Timeout and limits on CPU, memory, processes and disk.

**How it is satisfied.** Every command has a timeout (default 600 s; the gateway limits
a call to 300 s). The child process gets limits on address space (3 GiB), processes
(256), file size (512 MiB) and core dumps (0). The workspace is capped at 2 GiB and
checked after each command. The container group has a fixed 2 CPU and 4 GB allocation
for the server and the policy checks that against the approved resources.

**Evidence.** `test_workspace_size_cap`; live: process bomb, memory bomb and oversized
file were all stopped.

**Residual.** None.

#### 20. Restricted outbound access

**Meaning.** No arbitrary internet, package installs, unrelated corporate systems, or
cloud metadata and management credentials.

**How it is satisfied.** The worker subnet's NSG denies Internet and virtual network
egress. Only DNS, attestation, Entra and registry pulls are allowed. Generated code
also runs in an empty network namespace (a new user and network namespace created
after privileges are dropped, fail-closed), so it has no network interface at all.
This blocks the link-local instance metadata endpoint, which an NSG cannot. Only
`pip_install` and `git_clone` keep the network, and the NSG blocks general package and
Git egress for them. Packages are baked into the approved, digest-pinned image.

**Evidence.** `infra/pool/bicep/nsg.bicep`; live `hostile` phase: `internet` and
`metadata_service` were both blocked.

**Residual.** None for generated code. The worker holds a registry-pull identity, but
code it runs cannot reach the endpoint that would issue its token.

#### 21. Protected temporary data

**Meaning.** No persistent plaintext. State where temporary files live, how they are
protected, and what remains after destruction.

**How it is satisfied.** No volume is mounted (the policy forbids it). Files live on
the container's ephemeral filesystem, which is backed by encrypted TEE memory and
discarded when the group is deleted. The workspace is size-capped for that reason.

**Evidence.** Template and policy shape tests; live `storage` phase: the workspace is
on an overlay filesystem with no block-device or Azure Files mounts, a marker file
written in one session was not found by a session on a new worker group.

**Residual.** The claim that nothing persists after deletion rests on platform design;
the live check shows no mounted persistent storage and no carry-over, not what Azure
does to the underlying host storage.

#### 22. Direct result delivery

**Meaning.** Return results through the protected channel, not through logs or
persistent intermediary storage.

**How it is satisfied.** Results are sealed in the worker with the session key and
returned through the same encrypted channel. Nothing is staged in storage, a queue or
a log. Result size is bounded.

**Evidence.** `test_all_tools_routed_results_and_errors_unchanged`; live download
verified.

**Residual.** Results pass through the gateway in plaintext (accepted).

### Logging, deployment and assurance

#### 23. Content-free operational telemetry

**Meaning.** Record lifecycle, verification outcomes, durations, sizes and error
categories, never contents.

**How it is satisfied.** The gateway and worker write no per-request log lines, and
tool errors are not logged, so file names, code, stdout and bodies never appear. The
smoke tool prints outcome labels only.

**Evidence.** Code review of `server.py` and `gateway.py`; `test_all_tools_routed_results_and_errors_unchanged`.

**Residual.** Platform logs (Container Apps, Azure Activity) are metadata. Keep
diagnostic settings governed by the security authority.

#### 24. Approved, immutable deployment

**Meaning.** A known image and configuration, identified immutably; an unapproved
replacement must not receive secrets.

**How it is satisfied.** Images are referenced by digest. `make_policy_base.py` and
`finalize_policy.py` generate the CCE policy from those digests and the approved
configuration. The policy hash is signed by the hardware and checked by the gateway
before a key is released, so a different image cannot receive one.

**Evidence.** `test_base_policy_injection_binds_images_and_shape`,
`test_finalize_is_local_only`. The approved digests and policy hash are in the release
approval record.

**Residual.** The release approval record has been signed by the human approver. Any
digest or policy change invalidates it and requires a new signature.

#### 25. Demonstrable enforcement

**Meaning.** Prove the controls with a working prototype and negative tests.

**How it is satisfied.** A working end-to-end deployment, 130 automated tests, and live
`basic`, `hostile`, `storage` and `fault` smoke phases.

| Negative case | Where covered |
|---|---|
| Unauthorized or cross-owner access | Tests; live rejected |
| Modified or substituted environment | `test_substitute_worker_evidence_is_rejected`, `test_base_policy_injection_binds_images_and_shape` |
| Replay | `test_session_key_wrap_and_encrypted_channel` |
| Gateway restart | `test_gateway_restart_cannot_resume_a_keyed_session`; live `fault` (gateway restart retires the keyed session, no replay) |
| Interruption or uncertain outcome | `test_gateway_uncertain_outcome_no_replay_and_release_race`; live `fault` (worker group deleted mid-session: retired, no replay, pool recovers) |
| Sandbox escape paths | `test_generated_code_cannot_see_worker_secrets`; live `hostile` |
| Cleanup | `test_orphan_cleanup_selects_only_old_groups_of_this_pool`; live |

**Residual.** Replay and substitute-worker injection have not been run against the live
pool, because they disrupt it. They are covered by automated tests.

## Evidence: tests and live checks

- **Automated tests.** Worker and channel tests are in `tests/test_pool_feasibility.py`;
  controller, gateway and lease tests are in `tests/test_pool_control.py`. Offline
  tests use explicit fake Azure and attestation services, so they prove logic, not live
  availability.
- **Live smoke.** `infra/pool/smoke.py`, run on 2026-10-24 with two separate callers.
  `basic`: create, activate, upload, pandas run, download, delete, cross-owner
  rejection and use-after-delete rejection. `hostile`: adversarial code (non-root, no
  secrets, server files blocked, no internet or metadata service, process, memory and
  file size limits, cross-session read rejected). `storage`: no persistent mounts and
  no carry-over to a new group. `fault`: a worker group deletion and a gateway restart
  each retire the affected session without replay, and a fresh session then succeeds.
- **VPN end-to-end.** On 2026-10-24 `examples/pooled_analysis.py` ran from an
  on-premises machine over the VPN, with the gateway certificate pinned. It created a
  session, uploaded a spreadsheet, ran pandas, downloaded the result, and released the
  session. The output matched the expected values.
- **Release gate.** Follow the [live release gate](operations.md#live-release-gate)
  before claiming a successful deployment.

## Open items for sign-off

1. The release approval record for the exact image and policy digests is signed
   (requirement 24). Re-sign after any image or policy change.
2. Accept the documented boundaries (administrator trust, plaintext through the
   gateway, and the platform-level deletion claim for requirement 21).
3. Complete the production release gates (load and scale tests, reliable on-premises
   connectivity, certificate rotation, monitoring) before broader use.
