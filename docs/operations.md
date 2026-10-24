# Operations guide

Use an operator-approved environment and synthetic/low-sensitivity data until
the open items in the [requirements assessment](requirements-assessment.md) are
accepted. This guide is not an availability promise.
[Setup and authentication](getting-started.md) and
[architecture](ARCHITECTURE.md) explain prerequisites.

## Recorded evidence vs current state

The recorded live runs covered create, spreadsheet upload, pandas analysis,
result download, release, cross-owner denial, the per-session encrypted channel
and hostile-code checks. Separate two-worker probes checked file/session/network
separation and retirement.

The recorded configuration was 3 maximum workers and 2 desired ready workers.
Setting desired ready to zero stops provisioning warm workers but **does not
delete existing workers**. No current Azure inventory is claimed by this guide.
The 10-ready/20-maximum target still needs load/failure/capacity acceptance.

## Configured limits

| Setting | Value / interpretation |
|---|---|
| Capacity | 3 total groups; ready/provisioning/active/draining/deleting count toward the cap |
| Ready target | 2 when enabled; best effort, not extra capacity beyond the cap |
| Lifetime | 30 minutes idle, 4 hours absolute |
| Queue | 20 requests, 600-second wait |
| Operation | 300 seconds maximum |
| Transfer | 1 MiB configured serialized bounds; base64/JSON overhead reduces usable file size |
| Timing | About 1 minute warm acquisition, 3.5-4 minutes cold provisioning; not an SLA |
| Egress | No general internet/package/Git access; restricted Azure-service dependencies |

When capacity is exhausted, `create_session` returns a queued session. Poll
until active; expired/failed/closed is terminal. At the cap, admission waits for
retirement and replacement, not merely a slot being released. Do not rely on
queueing instead of sizing for bursts, or blindly retry unknown-outcome code.

## Connect and use

Connect through approved on-premises private routing or VPN. Confirm the private
gateway hostname resolves to the operator's intended private ingress and HTTPS
is reachable. A public DNS response, routing failure, and missing API role are
different problems. Do not use Azure's platform DNS virtual IP directly as a
VPN client resolver; use your reachable resolver/inbound endpoint.

Pin the ingress certificate against a trusted published fingerprint; see
[getting started](getting-started.md#verify-and-pin-gateway-tls).
Configure `examples\.env`, then use the
[direct MCP notebook](../examples/01_mcp_tools.ipynb),
[Agent Framework notebook](../examples/02_agent_framework.ipynb), or scripts.

Clients release sessions in cleanup. If a process/kernel dies or the network
breaks, use `list_sessions` and explicitly release unused owned sessions.
TTL expiry is a backstop, not proof of immediate Azure group deletion.
Release acknowledgement means destruction is scheduled. Operators must inspect
controller state/inventory for leftovers.

## Admins: assign the app role

Use IDs from **your** tenant. The API service principal object ID differs from
its application/client ID. An assignment's principal ID is a user, group or
client service principal **object** ID, not its application ID.

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

List/delete assignments using that API's `appRoleAssignedTo` collection and
assignment ID. Grant administrative consent for the caller app as appropriate.
Direct human sign-in needs additional client/API configuration; the default
examples use the trusted application's credentials.

## Stop, resume, and cost control

An authorized operator can redeploy the apps stage with the **same explicit
environment, approved image/attestation inputs** and `-DesiredReady 0` to stop
new warm-worker provisioning. To resume use the approved ready target and cap.
See [deployment parameters](../infra/pool/README.md).

Ready target zero does not delete existing ready/active groups. Review ownership
and worker state, release sessions and deliberately remove leftovers. For a
complete stop, quiesce clients, verify no required work remains, then stop
gateway/controller replicas and clean up pool-tagged workers using explicit
resource names. Never delete unrelated shared VNet/registry resources.

At the historical rate, two continuously warm workers alone cost about
$0.32/hour. Gateway/controller and shared networking/storage remain billable;
see the [illustrative cost baseline](session-pool-costs.md).

## Live release gate

**Not performed by the offline checks.** Obtain approval for Azure/model charges
and an operator-reviewed environment before running:

1. Confirm deployed images match intended source and approved CCE digests.
   Rebuild the worker and regenerate policy if source changed; do not assume the
   historical image contains the cleaned source.
2. Check private DNS/routing, certificate fingerprint, sandbox role/audience,
   and separate Foundry credentials/model permissions from the on-premises host.
3. Execute notebook 1 from a clean kernel using only the bundled synthetic data.
   Verify CSV rows `count/mean/sum`, revenue `4/160/640`, units `4/16/64`.
4. Execute notebook 2 and the runnable Agent Framework script. Confirm actual
   MCP execution, local artifact validation and normal release, not just a
   model-generated success message.
5. Test denied cross-principal access and bounded queue/expired/error paths.
   Confirm normal failure cleanup and define kernel/network-loss recovery.
6. Verify controller admission logs/evidence against the approved attestation
   baseline, and verify physical worker destruction/no leaked pool groups.
   Run the `basic` and `hostile` phases of `infra/pool/smoke.py` (as a job inside
   the VNet with two caller identities) against the exact deployed digests.
   Check the scheduled orphan cleaner is enabled, the gateway is a single
   replica, and the release approval record is signed for these digests.
7. Clear notebook execution outputs before publication. Record configuration
   and results privately without publishing tokens, secrets or environment IDs.

Before scaling or production: load/burst/failure tests, quota/capacity review,
reliable on-premises connectivity, certificate/key rotation, monitoring and budget
alerts, and an explicit decision on SMT and pull-identity residual risks.
