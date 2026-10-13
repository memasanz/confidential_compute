# Confidential session pool - pilot guide

## What the pilot is

A small shared pool of **confidential workers** (Azure Container Instances, AMD SEV-SNP) behind a private
MCP gateway. You ask for a **session**; the pool gives you a dedicated, remotely attested worker, you upload
a spreadsheet, run Python (pandas/openpyxl preinstalled) and download the result. When you delete the
session the worker is destroyed - the next user always gets a fresh worker.

The gateway authenticates you with an Entra token (audience = the API app, role `Sandbox.Access`) and only
lets you see and use your own sessions. It never holds the pool CA or worker keys.

## Limits

| Item | Pilot value |
|---|---|
| Concurrent sessions (all users together) | **3** |
| Workers kept warm | 2 (a session starts in about 1 minute) |
| Session lifetime | **4 h absolute, 30 min idle** |
| Cold start (no warm worker free) | **about 4 minutes** (create + boot + attestation) |
| Queue | up to 20 waiting; a waiting session expires after **10 minutes** |
| Upload / download | 1 MiB each |
| Per command | 300 s maximum |
| Network from the worker | **None by design**: no `pip install`, no `git clone`, no internet |

**Queue behavior.** If all 3 sessions are in use, `create_session` still succeeds and the session is
`queued`. It becomes `active` when someone releases a session and a replacement worker is ready (about 4 minutes
after the release). If that does not happen within 10 minutes the session becomes `expired`: create a new one
later. Always delete your session when finished - it frees the slot for others.

**SMT residual risk.** The approved attestation baseline allows hosts with simultaneous multithreading (SMT)
enabled, as the current ACI confidential hosts report it. SMT leaves a residual side-channel risk between
hardware threads on the same physical core that SEV-SNP memory encryption alone does not remove. The pilot
accepts this for synthetic or low-sensitivity spreadsheets; do not use data whose protection depends on
excluding co-resident side channels.

## Prerequisites

1. **Entra identity with the app role.** An admin assigns `Sandbox.Access` (see below). Users may use a
   service principal (client secret in `examples\.env`); signing in as yourself with `az login` additionally
   needs the API app to pre-authorize Azure CLI, which is **not configured** in the pilot.
2. **VPN.** The gateway is private (`192.168.5.23`, subnet `snet-pool-control`).
   - Gateway: `vpngw-fabric-foundry`, OpenVPN with Entra (Azure AD) authentication, client pool `172.16.0.0/24`.
   - Install the *Azure VPN Client* and import the profile. Admins can download it from the Azure portal
     (Virtual network gateway -> Point-to-site configuration -> Download VPN client) and send the profile
     (not secret, but internal) to approved users.
   - **DNS:** VPN clients use the VNet DNS server `192.168.1.36`, which resolves the private zone
     `ambitiouscoast-29e52285.eastus2.azurecontainerapps.io` (A `@` and `*` -> `192.168.5.23`). Verified from
     inside the VNet: `192.168.1.36` answers `pool-gateway.<domain>` with `192.168.5.23`.
   - **Routing/NSG:** no change was needed. The control subnet's NSG has only the default rules
     (`AllowVnetInBound` covers the VPN client pool because the gateway makes it part of the VirtualNetwork tag),
     there is no route table, and ACA internal ingress listens on 443. The worker NSG is untouched.
   - Not verified from a real VPN client in the pilot-prep run: after connecting, confirm with
     `Resolve-DnsName pool-gateway.ambitiouscoast-29e52285.eastus2.azurecontainerapps.io` and
     `Test-NetConnection 192.168.5.23 -Port 443`. If DNS fails, set the VPN client's DNS to `192.168.1.36`.
3. **Python 3.11+** with `pip install -r examples\requirements.txt` (on Microsoft-managed devices use the package
   proxy, `https://packagefeedproxy.microsoft.io/pypi/simple`, as `--index-url`).

## Trust the gateway certificate (pinning)

The internal ingress presents the environment's **self-signed** certificate. You trust exactly that
certificate instead of the public CA store.

Admin (once, on the VPN): print and publish the fingerprint.

```powershell
python examples\pin_gateway_cert.py https://pool-gateway.ambitiouscoast-29e52285.eastus2.azurecontainerapps.io/mcp --out gateway-cert.pem
```

User: verify against the published fingerprint (the file is written only if it matches).

```powershell
python examples\pin_gateway_cert.py https://pool-gateway.ambitiouscoast-29e52285.eastus2.azurecontainerapps.io/mcp --expect <sha256> --out gateway-cert.pem
```

Current fingerprint (pilot): `470a7c424cedcd6670697eac6d1e10233bcf642e5ad317179585b96944318126`.
If the environment's certificate is rotated the fingerprint changes: re-run the admin step and republish.

## Admins: assign the app role

API app `mcp-confidential-sandbox-api` (`cee796a9-d196-4e24-86f3-807f9e669625`), role `Sandbox.Access`
(`5978724a-d5fb-4530-9fd8-33301b7adc73`), service principal object id `cc32b538-cb6b-42ca-9c6d-3bd9ef443522`.
`<principal-object-id>` is the user's, or the service principal's, object id.

```powershell
'{"principalId":"<principal-object-id>","resourceId":"cc32b538-cb6b-42ca-9c6d-3bd9ef443522","appRoleId":"5978724a-d5fb-4530-9fd8-33301b7adc73"}' | Set-Content assign.json
az rest --method POST --headers "Content-Type=application/json" `
  --uri https://graph.microsoft.com/v1.0/servicePrincipals/cc32b538-cb6b-42ca-9c6d-3bd9ef443522/appRoleAssignedTo `
  --body "@assign.json"
```

List or remove assignments with `GET` and `DELETE .../appRoleAssignedTo/<assignment-id>` on the same URI.
The existing client app `mcp-onprem-agent` (`8a175fce-010a-4d0b-8692-17248403a146`, service principal object id
`5ac725ad-1e1f-4148-aced-69b7920eca73`) can be reused the same way. The gateway rejects tokens without the role.

## Run the pooled example

1. Copy `examples\.env.example` to `examples\.env` (never commit it) and set `MCP_TENANT_ID`, `MCP_CLIENT_ID`,
   `MCP_CLIENT_SECRET`, `MCP_API_APP_ID`, and add:
   ```
   POOL_GATEWAY_URL=https://pool-gateway.ambitiouscoast-29e52285.eastus2.azurecontainerapps.io/mcp
   POOL_GATEWAY_CA=gateway-cert.pem
   MCP_EXCEL_PATH=sample_sales.xlsx   # optional: your own file, 1 MiB max
   ```
2. Connect the VPN, then `python examples\pooled_analysis.py`.

The script: `create_session` -> poll `get_session_status` until `active` (handles queued/expired) ->
`upload_file` -> `run_python` -> `download_file` (saves `summary.csv`) -> `delete_session` in a `finally`
block. Tools available to build your own flow: `create_session`, `get_session_status`, `list_sessions`,
`delete_session`, `upload_file`, `download_file`, `write_file`, `read_file`, `list_dir`, `run_python`,
`run_command`, `run_tests`, `get_attestation`. (`pip_install` and `git_clone` exist but fail: no egress.)

## Ending sessions

Call `delete_session` (the example does it automatically). `list_sessions` shows your sessions if a script
crashed. Unused sessions end by themselves after 30 minutes idle or 4 hours.

## Operations: who and how to stop

Contact the pool owner (the team that deployed `rg-confidential-pool-control`). Admin commands:

```powershell
# Scale warm workers to 0 (stops worker cost; apps stay up). Run from infra\pool.
.\deploy-pool-apps.ps1 -Stage apps -ImageDigest <digest in .azure\pool-pilot\digest.txt> -DesiredReady 0 -MaxTotal 3 -QueueWait 600
# Resume
.\deploy-pool-apps.ps1 -Stage apps -ImageDigest <digest> -DesiredReady 2 -MaxTotal 3 -QueueWait 600
# Full stop (no gateway, no controller)
az containerapp update -g rg-confidential-pool-control -n pool-controller --min-replicas 0 --max-replicas 0
az containerapp update -g rg-confidential-pool-control -n pool-gateway --min-replicas 0 --max-replicas 0
```

Scaling `DesiredReady` to 0 stops creating warm workers; existing ready workers are not deleted. Delete leftovers
with `az container delete -g rg-confidential-pool -n <pool-...> --yes`.

## Cost

Estimated, public list prices, East US 2:

| Item | Per hour |
|---|---|
| 2 warm confidential workers (about $0.16 each) | $0.32 |
| Each active session beyond the warm ones | about $0.16 while it exists |
| Controller (1 vCPU / 2 GiB) + gateway (0.5 vCPU / 1 GiB) Container Apps | about $0.10-0.16 |
| **Idle pilot total** | **about $0.45-0.50 per hour (about $11/day)** |

The shared VPN gateway and storage/DNS are not included.

## Known issues

- Cold replacement is about 4 minutes; sessions queued behind a full pool can expire (10 min).
- The pool-wide cap is 3 sessions during the pilot; the approved production cap is larger and a config value.
- The ARM delete of a worker is sometimes slow (6-67 s); a slot is only reused after delete is confirmed.
- The controller logs an occasional `Pool reconciliation unavailable (EOFError)`; it retries on the next cycle.
- The gateway certificate is self-signed and must be pinned; there is no public CA chain.
- No package or git egress: bring data and code in with `upload_file`.
