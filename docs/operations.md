# Operations guide

This guide is for the people who **run** the service: connecting to it, giving
people access, controlling cost, rolling out new versions and keeping certificates
current. If you are new to the solution, read the overview first.

Use an operator-approved environment and synthetic or low-sensitivity data until
the open items in the [requirements assessment](requirements-assessment.md) are
accepted. This guide is not an availability promise.

## Find your task

| I want to... | Go to |
|---|---|
| Understand how the service works | [Overview](#overview) |
| Know the limits (capacity, timeouts, queue) | [Limits](#limits) |
| Connect and run an analysis | [Connect and use](#connect-and-use) |
| Give a person or app access | [Give someone access](#give-someone-access) |
| Pause the pool or stop paying for warm workers | [Stop, resume and cost control](#stop-resume-and-cost-control) |
| Ship a new version of the worker | [Rolling out a new worker image](#rolling-out-a-new-worker-image) |
| Renew certificates or fix an expired one | [Certificate rotation](#certificate-rotation) |
| Prove a deployment works before relying on it | [Live release gate](#live-release-gate) |

Prerequisites are in [setup and authentication](getting-started.md); the design is in
the [architecture](ARCHITECTURE.md).

## Overview

A caller on your network asks a private **gateway** for a **session**. Each session
runs in its own **confidential container group**: a worker in a hardware-protected
environment (AMD SEV-SNP) that Azure cannot read. A **controller** keeps a few
workers warm, hands one to each session, and destroys it afterwards. A worker is never
reused.

You will see these terms:

- **Session:** one analysis, owned by one caller, using one worker.
- **Gateway:** the private MCP server your agent talks to. It checks the caller's
  identity and ownership, then forwards each tool call to that session's worker over
  an encrypted, attested channel. It is not inside the confidential environment, so it
  can see the data it routes. Callers cannot choose a worker; the gateway picks the
  one belonging to the session.
- **Controller:** the service that creates, verifies and destroys workers.
- **Warm (ready) worker:** a worker already started and verified, waiting for a session.
- **Attestation:** hardware-signed proof of what a worker is running. The controller
  checks it before a worker receives any work.
- **Digest:** the exact fingerprint of a container image, written `sha256:...`.
  Images are referenced by digest, not by a changeable tag.
- **Operational directory:** the private folder (for example `.azure\pool-pilot`)
  holding your certificates, policy and deployment parameters. It is not committed.

### What has been tested

Live runs on the recorded configuration covered create, spreadsheet upload, pandas
analysis, result download, release, cross-owner denial, the per-session encrypted
channel and hostile-code checks. Separate two-worker probes checked file, session and
network separation and retirement.

The recorded configuration was **3 maximum workers and 2 desired ready workers**. The
10-ready/20-maximum target still needs load, failure and capacity acceptance. This
guide does not claim a current Azure inventory; check the real state before acting.

## Limits

| Setting | Value and meaning |
|---|---|
| Capacity | 3 total groups. Ready, provisioning, active, draining and deleting groups all count toward the cap |
| Ready target | 2 when enabled. Best effort; it never adds capacity beyond the cap |
| Lifetime | 30 minutes idle, 4 hours absolute |
| Queue | 20 requests, each waiting up to 600 seconds |
| Operation | 300 seconds maximum per call |
| Transfer | 1 MiB serialized bounds; base64/JSON overhead reduces the usable file size |
| Timing | About 1 minute to get a warm worker, 3.5 to 4 minutes for a cold one. Not an SLA |
| Network | No general internet, package or Git access; only the Azure services the worker needs |

### What happens when the pool is full

`create_session` returns a **queued** session. Poll `get_session_status` until it is
`active`. `queued` is not ready. `expired`, `failed` and `closed` are final. At the
cap, a queued session waits for a worker to be destroyed **and** a replacement to be
verified, not just for a slot to open. Do not rely on the queue instead of sizing the
pool for bursts, and never blindly retry code whose outcome is unknown. The
[lease table and queue](isolated-session-pool.md#lease-table-and-session-queue)
section explains the mechanics.

## Connect and use

1. **Reach the gateway privately.** Use approved on-premises private routing or a
   VPN. Confirm the gateway hostname resolves to the private ingress you expect and
   that HTTPS is reachable. A public DNS answer, a routing failure and a missing API
   role are three different problems. Do not use Azure's platform DNS address
   directly as a VPN client resolver; use your reachable resolver or inbound endpoint.
2. **Pin the gateway certificate.** Compare it with a fingerprint the operator
   published over a trusted channel. See
   [getting started](getting-started.md#verify-and-pin-gateway-tls).
3. **Configure and run.** Set up `examples\.env`, then use the
   [direct MCP notebook](../examples/01_mcp_tools.ipynb), the
   [Agent Framework notebook](../examples/02_agent_framework.ipynb) or the scripts.

### Cleaning up after a failure

Clients release their sessions in a cleanup step. If a process or kernel dies or the
network breaks:

- Call `list_sessions` and release any unused sessions you own.
- Session expiry is a backstop, not proof that the Azure group is already gone.
- A release acknowledgement means destruction is **scheduled**. Operators must check
  controller state and the real inventory for leftovers.

## Give someone access

Callers need the `Sandbox.Access` app role on the sandbox API. A tenant administrator
assigns it. Use IDs from **your** tenant, and note the two kinds of ID:

- The API's **object ID** (the service principal) is different from its **application
  (client) ID**.
- The assignment's `principalId` is the **object ID** of the user, group or client
  service principal, never its application ID.

```powershell
# Set these explicitly; do not paste secrets into source or terminal transcripts.
$apiServicePrincipalObjectId = '<API_SERVICE_PRINCIPAL_OBJECT_ID>'
$callerObjectId = '<CALLER_OBJECT_ID>'
$sandboxAccessRoleId = '<SANDBOX_ACCESS_APP_ROLE_ID>'
$body = @{
    principalId = $callerObjectId
    resourceId = $apiServicePrincipalObjectId
    appRoleId = $sandboxAccessRoleId
} | ConvertTo-Json -Compress
$assignmentFile = Join-Path $env:TEMP 'confidential-pool-role-assignment.json'
$body | Set-Content -Encoding utf8 $assignmentFile
az rest --method POST --headers 'Content-Type=application/json' `
    --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$apiServicePrincipalObjectId/appRoleAssignedTo" `
    --body "@$assignmentFile"
Remove-Item $assignmentFile
```

To list or remove assignments, use the same API's `appRoleAssignedTo` collection and
the assignment ID. Grant administrator consent for the caller app as appropriate.
Direct human sign-in needs extra client and API configuration; the default examples
use the trusted application's own credentials.

## Stop, resume and cost control

**Pause new warm workers.** Redeploy the apps stage with the **same explicit
environment and approved image and attestation inputs**, plus `-DesiredReady 0`.
To resume, redeploy with the approved ready target and cap. Parameters are in the
[deployment guide](../infra/pool/README.md).

`-DesiredReady 0` does **not** delete workers that already exist. Review ownership
and worker state, release sessions and deliberately remove leftovers.

**Full stop.** Quiesce clients, confirm no required work remains, stop the gateway
and controller replicas, then clean up pool-tagged workers by explicit resource name.
Never delete unrelated shared network or registry resources.

**Cost.** At the historical rate, two continuously warm workers alone cost about
$0.32 per hour. The gateway, controller and shared networking and storage stay
billable. See the [illustrative cost baseline](session-pool-costs.md).

## Rolling out a new worker image

Do this whenever the worker source changes. Complete every step: a skipped step leaves
the pool silently stopped or failing closed.

1. **Build** the worker image in the registry and record its digest.
2. **Regenerate the base policy** for that digest with `make_policy_base.py`.
   - Use the exact sidecar reference from the running controller.
   - Use the pull-identity resource ID with its original `resourceGroups` casing.
     Copy it from the controller's environment, not from `az identity list`, which
     lowercases it and makes the controller fail closed.
   - Keep the old base policy as a backup.
3. **Build the `pool-control` image** (stage `build`) so the new base policy is inside it.
4. **Preview:** run the apps stage with `-WhatIfOnly`. Only the two container apps
   should change.
5. **Deploy:** run it for real, **passing the same `-DesiredReady` and `-MaxTotal` as
   before**. The default `-DesiredReady` is 0 (the pause setting), so leaving it out
   silently stops the pool from creating workers.
6. **Delete the old worker groups** (pool-tagged only). Old workers run the old image
   and would still be handed to sessions.
7. **Verify:**
   - The controller's `POOL_DESIRED_READY` and `POOL_MAX_TOTAL` values are what you
     intended.
   - New worker groups appear and reach `Succeeded`, and the controller log shows
     provisioning. Ticks reporting "0 operations" with no new groups mean the ready
     target is 0.
   - Each worker runs the new image digest.
   - The `basic` and `hostile` smoke phases pass on the new build.
8. **Re-approve:** regenerate the release approval record for the new digests. A
   person must sign it.

## Certificate rotation

### What expires

| Credential | Where | Lifetime | What happens at expiry |
|---|---|---|---|
| Gateway client CA and client certificate | `pki\` in the operational directory; deployed to the gateway and controller | `make_pki.py --days` (default 90) | The gateway cannot open mutual TLS to workers; sessions fail closed |
| Gateway HTTPS certificate | Gateway ingress; platform-managed, issued by a Microsoft TLS CA | About 100 days, renewed by the platform | The fingerprint changes on renewal; clients that pin the old one fail TLS verification |
| Worker TLS key | Generated inside each worker | One session | Nothing; no rotation needed |

Nothing in the pool monitors expiry, so **the check is manual**. At least monthly,
check the dates with the command below for `gateway-client.pem` and `client-ca.pem`,
and set a calendar reminder 30 days before expiry. Also compare the fingerprint your
clients pin with the one the gateway presents (`examples\pin_gateway_cert.py` from a
trusted network); a mismatch means the platform renewed the HTTPS certificate.

```powershell
.\.venv\Scripts\python.exe -c "import sys; from cryptography import x509; c = x509.load_pem_x509_certificate(open(sys.argv[1], 'rb').read()); print(c.not_valid_after_utc)" "$operationalDirectory\pki\gateway-client.pem"
```

### Rotating the gateway client certificate

Rotate at least 14 days before expiry.

1. **Quiesce clients.** Rotation retires every active session, because the gateway
   restarts.
2. **Generate new certificates.** Move the old `pki\` directory aside as a backup
   (`make_pki.py` refuses to overwrite files), then run
   `make_pki.py --output "$operationalDirectory\pki"`.
3. **Redeploy** the apps stage with the **same** parameters as before, including
   `-DesiredReady` and `-MaxTotal`. Preview with `-WhatIfOnly` first.
4. **If the client CA changed,** confirm the controller still accepts the approved
   policy hash. If it logs a policy mismatch, regenerate the policy base and rebuild
   as in the worker-image rollout, then delete the old workers.
5. **Verify:** the new gateway revision is healthy, workers reach `Succeeded` and the
   `basic` smoke phase passes.
6. **Delete the old keys** securely once nothing depends on them.

Keep `client-ca.key` offline; it is needed only to issue new client certificates.

### When the gateway HTTPS certificate renews

The platform renews it, so this is routine rather than something you trigger. Each time
it changes:

1. From inside the VNet, read the new fingerprint (run `examples\pin_gateway_cert.py`
   without `--expect`, or read it in a job as the smoke phases do).
2. Publish it over a trusted channel.
3. Have clients re-pin (see [getting started](getting-started.md)).

Never switch to `verify=False` to get past a fingerprint error.

### If a certificate has already expired

- **Client CA or client certificate expired:** the gateway cannot reach workers, so new
  sessions fail and existing ones are retired. Skip the quiesce step (nothing usable is
  running), delete the failed pool workers, follow steps 2 to 5 above and confirm a
  fresh session activates and runs code.
- **Gateway HTTPS certificate changed:** pinned clients fail with a fingerprint
  mismatch and no session is created. The pool itself is healthy; republish the new
  fingerprint and re-pin clients. The last measured certificate was issued 2026-10-12
  and expires 2027-01-20, so expect a renewal before then.

## Live release gate

The offline tests do **not** prove a deployment works. Before claiming a successful
deployment, get approval for Azure and model charges and an operator-reviewed
environment, then work through these checks:

1. **Right images.** Confirm the deployed images match the intended source and the
   approved policy digests. Rebuild the worker and regenerate the policy if source
   changed; do not assume an old image contains the cleaned source.
2. **Connectivity and identity.** Check private DNS and routing, the certificate
   fingerprint, and the sandbox role and audience. Confirm separate Foundry
   credentials and model permissions on the on-premises host.
3. **Notebook 1.** Run it from a clean kernel using only the bundled synthetic data.
   Verify the CSV rows (`count/mean/sum`), revenue `4/160/640` and units `4/16/64`.
4. **Notebook 2 and the Agent Framework script.** Confirm real MCP execution, local
   artifact validation and a normal release, not just a model-written success message.
5. **Denial and error paths.** Test that cross-principal access is denied and that the
   queue, expiry and error paths are bounded. Confirm failure cleanup works and decide
   how to recover from a kernel or network loss.
6. **Evidence and destruction.** Check controller admission logs against the approved
   attestation baseline, and verify that workers are physically destroyed with no leaked
   pool groups. Run the `basic` and `hostile` phases of `infra/pool/smoke.py` as a job
   inside the VNet with two caller identities, against the exact deployed digests.
   Also check that the scheduled orphan cleaner is enabled, the gateway is a single
   replica, and the release approval record is signed for these digests.
7. **Publication hygiene.** Clear notebook outputs before publishing. Record
   configuration and results privately, without tokens, secrets or environment IDs.

## Before scaling or production

Complete these first: load, burst and failure tests; a quota and capacity review;
reliable on-premises connectivity; certificate and key rotation procedures;
monitoring and budget alerts; and an explicit decision on the SMT and pull-identity
residual risks.
