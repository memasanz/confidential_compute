# Getting started: private-network confidential sandbox

This guide describes the private-VNet deployment and onboarding model used by
the notebooks. It is a guided setup, not a fully automated installer. No users,
app registrations, certificates, VPN profiles, or Azure resources are created
automatically by opening a notebook.

**Choose the execution boundary first.** This prototype protects a confidential
container group's memory from the infrastructure host. Its per-session
directories and virtualenvs do not contain hostile code. Use one group per trust
domain, or choose Container Apps dynamic sessions when isolation between
untrusted users is the primary requirement.

## What belongs to the service, the application, and the user?

| Item | Who needs it? | Purpose |
|---|---|---|
| HTTPS server certificate and private key | One per sandbox service/DNS name, with planned renewal | Authenticate and encrypt the HTTPS connection |
| Sandbox API app registration | One per API/environment | Defines the audience and application permission |
| Client/agent app registration | One per trusted calling application/environment | Identifies the application calling the sandbox |
| Client secret or optional client-app certificate | The calling application, not every human user | Lets the application obtain an Entra token |
| Entra user identity | Each human user | Signs in to the front-end and, if configured, the VPN |
| VPN client profile | Each workstation using point-to-site VPN | Provides a route into the VNet |
| Per-user VPN certificate | Only if the VPN uses certificate authentication | Not needed for an Entra-authenticated VPN |
| ACI user-assigned managed identity | The confidential container group | Releases its key from Managed HSM |

Do **not** create an app registration or share a daemon's client secret with every
user. App registrations represent applications; human users use their directory
accounts. The service certificate, app-authentication certificate, and VPN
certificate are different credentials. The MAA attestation token is not any of
these certificates.

## 1. Prepare the repository and administrative access

Clone the repository and follow **Initialize a fresh checkout** in the README to
copy the sanitized examples to local configuration files:

- `infra\template-vnet.json`
- `infra\parameters-vnet.json`
- `infra\skr-release-policy.json`
- `examples\.env`

Keep configured JSON, `.env`, certificates, and private keys out of Git. Do not
overwrite existing local configuration when copying examples.

Install Azure CLI, its `confcom` extension, and Docker in Linux-container mode.
Install the notebook dependencies using the README's `.venv` instructions.
Sign in with `az login` and confirm the subscription before provisioning.
Replace every `<...>` command placeholder with your own value before running it.

Administrative responsibilities may belong to different people:

| Responsibility | Required access |
|---|---|
| Deploy ACI, ACR, and network resources | Appropriate Azure management-plane permissions on the target resources |
| Register apps and grant API permission | Entra app-registration and admin-consent permissions |
| Activate/manage Managed HSM and its security domain | HSM administrator; follow your organization's recovery and custody procedures |
| Create/update SKR keys and release policies | Appropriate Managed HSM local data-plane role, such as Crypto User |
| Issue certificates and publish DNS records | Your PKI and DNS administrators |

Azure subscription Owner/Contributor does not automatically grant Managed HSM
local data-plane permissions.

## 2. Prepare the existing network and supporting resources

The private template deploys the container group into an **existing delegated
subnet**. It does not provision a VNet, VPN, ACR, or HSM.

Prepare:

1. A resource group and a region supporting confidential ACI.
2. An existing VNet with a dedicated ACI subnet delegated to
   `Microsoft.ContainerInstance/containerGroups`. Choose a nonoverlapping
   address range with enough capacity; current ACI guidance recommends `/24` or
   larger to avoid address-reuse failures. Do not reuse the private-endpoint or
   VPN gateway subnet. The reference script's smaller demo range is not the
   production sizing recommendation.
3. A NAT gateway associated with the ACI subnet for outbound connectivity.
   Restrict egress separately where your requirements demand it; NAT alone is
   not an egress allowlist.
4. An ACR containing the sandbox image. Private endpoints require an appropriate
   registry SKU and configuration.
5. An activated Managed HSM and a user-assigned managed identity.
6. If ACR/HSM are private, their private endpoints and private DNS zones linked
   to the VNet. Resolve registry image-pull endpoints and HSM endpoints privately.
7. Outbound connectivity/DNS for the configured Microsoft Azure Attestation
   endpoint, Entra token validation, and any permitted dependency feeds.
8. A route from the calling environment into the VNet: existing site-to-site
   connectivity, ExpressRoute, a point-to-site VPN, or a caller inside the VNet.
   Permit the required private TCP port, currently 8000, without exposing it to
   the internet.

`infra\deploy-vnet.ps1` illustrates creating the delegated subnet, NAT gateway,
and private endpoints. Review all resource names, parameter values, and command
overrides before using it; the supplied script is a reference workflow, not a
substitute for network planning.
Do not run it unchanged: normalize its MAA/HSM endpoint overrides to hostnames,
use the API application GUID for the audience, and ensure every deployment
override matches the values used when generating the CCE policy.

For workstation access, an Entra-authenticated point-to-site VPN uses **Azure VPN
Client**, a profile downloaded from the VPN gateway, and the user's Entra sign-in.
Users do not need personal VPN certificates for that mode. VPN connectivity
does not by itself grant access to the sandbox API.

For private DNS over VPN, use a reachable corporate DNS service or Azure DNS
Private Resolver as appropriate. Connecting to a VNet does not automatically
configure every private DNS name on the workstation.

## 3. Register the sandbox API and the calling application

These steps describe the app-only flow already used by the notebooks.

### Sandbox API registration

In **Microsoft Entra ID > App registrations**:

1. Register a single-tenant application for the sandbox API.
2. Record its **Application (client) ID** as `API_APP_ID`.
3. Under **Expose an API**, set the Application ID URI to `api://<API_APP_ID>`.
4. Under **App roles**, create an enabled role:
   display name/value `Sandbox.Access`; allowed member type **Applications**.
5. Set `api.requestedAccessTokenVersion` to `2` in the manifest.
6. Find the corresponding **Enterprise application**, open its properties, and
   enable **Assignment required**. This prevents unassigned applications from
   obtaining role-less app-only tokens for the API.

The current server validates token signature, issuer, audience, expiration, and
caller identity. It does **not** separately enforce the `Sandbox.Access` claim.
Do not assume that merely creating an app role makes the server check it:
retain the Entra assignment requirement, and implement explicit role/scope
authorization before expanding the API's permission model.

### Client/agent registration

1. Register a separate single-tenant application for the trusted caller.
   A daemon using client credentials does not need a redirect URI.
2. Record its Application ID as `CLIENT_APP_ID`.
3. Under **API permissions > Add a permission > My APIs**, select the sandbox
   API, choose **Application permissions**, and add `Sandbox.Access`.
4. Have an authorized administrator grant tenant-wide admin consent.
5. For the supplied notebooks, create a client secret and securely record its
   **Value** immediately. This is a secret from the **client app**, not the API
   app; its Secret ID is not a usable credential.

Configure the three identities consistently:

| Location | Setting | Value |
|---|---|---|
| Deployment parameters | `entraTenantId` | Tenant ID |
| Deployment parameters | `entraAudience` | API application ID **GUID**, not `api://...` |
| Notebook `.env` | `MCP_TENANT_ID` | Tenant ID |
| Notebook `.env` | `MCP_CLIENT_ID` | Client/agent application ID |
| Notebook `.env` | `MCP_CLIENT_SECRET` | Client app's secret Value |
| Notebook `.env` | `MCP_API_APP_ID` | API application ID GUID |

The notebook requests `api://<API_APP_ID>/.default`; with v2 tokens, the server
expects the GUID as the token's audience. The sandbox API app does not need a
client secret merely to validate incoming tokens.

### Optional: certificate authentication for the client application

For production, consider a client-app certificate, workload federation, or
managed identity instead of a long-lived shared secret:

- Generate a dedicated app-authentication key pair using your approved process.
- Upload the **public certificate only** to the client app's
  **Certificates & secrets > Certificates**.
- Keep the private key on the trusted caller, preferably in an approved
  credential store. Do not distribute it to end users.
- Configure the application to use certificate authentication. The supplied
  notebooks currently use `ClientSecretCredential`; using
  `ClientCertificateCredential` requires changing both the configuration cell
  (remove the required client-secret setting) and credential construction.

This optional certificate is not the HTTPS server certificate. Uploading it to
Entra does not enable HTTPS on the sandbox.

## 4. Obtain the HTTPS service certificate

Choose a stable private DNS name, for example `sandbox.corp.example`. Request a
**server-authentication** certificate from your organization's PKI, or use a
public CA where you control a suitable domain and can satisfy its validation
requirements. The certificate's Subject Alternative Name must contain that DNS
name; clients must connect using a matching name.

Obtain the PEM certificate chain and matching PEM private key and place them at:

```text
infra\tls\cert.pem
infra\tls\key.pem
```

Create the `infra\tls` directory if necessary, restrict access to the private
key, and arrange renewal. Do not upload the private key to users or Git.
The current demo uses a self-signed pair; it is not a production certificate
enrollment service. Azure does not automatically issue or renew this certificate
for the custom MCP server.

Deployment supplies the PEM contents as secure `tlsCert`/`tlsKey` parameters.
They become `MCP_TLS_CERT`/`MCP_TLS_KEY` inside the enclave, where Uvicorn terminates
HTTPS. These TLS values are not fetched through the SKR key-release mechanism.

After deployment, create or update a private DNS A record pointing the chosen
name at the group's current private IP. Ensure VPN and in-VNet callers resolve
it correctly. A private IP can change after redeployment, so DNS maintenance is
part of operating the service.

**Enable certificate verification before onboarding real data.** The example
notebooks use `verify=False` for their self-signed demo. Replace that setting in
their HTTPX client factory with verification against a trusted CA bundle. For a
corporate CA, supply an appropriate SSL context:

```python
import ssl

tls_context = ssl.create_default_context(cafile=r"C:\certs\corporate-root-ca.pem")
# In the existing HTTPX client factory, use verify=tls_context instead of False.
```

Use `MCP_ENDPOINT=https://sandbox.corp.example:8000/mcp`, not an IP that is absent
from the certificate's SAN. Distribute the **CA trust chain**, not the server
private key, to authorized clients.

The MAA attestation token is separate evidence about the workload. Fetching it
without verification does not prove identity. If you require an attestation-bound
connection before releasing data, implement and review that verification
protocol; the notebooks do not currently implement it.

## 5. Configure identity, build the image, and finalize policy

Grant the ACI managed identity **Managed HSM Crypto User** at the dedicated key
scope, or another appropriate least-privilege HSM scope. The operator creating
or updating the key also needs suitable HSM local permissions.

Fill in `infra\parameters-vnet.json`: subnet resource ID, image, registry,
managed identity resource ID, tenant ID, API audience GUID, and key name.
The key-name parameter is `mcpAuthKid`; it must match the key you create below.
MAA and HSM container endpoints must be **hostnames only**, without `https://`.
The release policy's `authority`, by contrast, is an HTTPS MAA URL.

Build and push the container image after reviewing its dependencies and tools:

```powershell
az acr build --registry <acr-name> --image mcp-build-sandbox:1.0 .\mcp-build-sandbox
az acr login --name <acr-name>
```

If private-registry restrictions prevent ACR Tasks from building/pushing, use
your approved network-connected build agent instead.

Ensure Docker is running so `confcom` can pull and hash the images. From `infra`:

```powershell
az confcom acipolicygen -a .\template-vnet.json -p .\parameters-vnet.json --approve-wildcards
.\require-tls-policy.ps1
```

Use `--disable-stdio` during generation if your approved policy requires blocking
container log access; this removes a debugging surface.

Do not put placeholder TLS values in the policy-generation parameters.
The finalizer makes their rules accept multiline PEM and requires both variables.
Copy its **final hash** into the `x-ms-sevsnpvm-hostdata` claim in
`skr-release-policy.json`, ensure the MAA authority matches your deployment,
then create the exportable SKR key:

```powershell
az keyvault key create --hsm-name <hsm-name> --name mcp-auth-token --kty RSA --size 2048 --exportable true --policy .\skr-release-policy.json
```

For an existing deployment, updating a mutable release policy can preserve the
key version. Plan the transition carefully so neither old nor new workloads lose
access unexpectedly; remove obsolete authorized hashes after a successful
cutover. Never bind the key to the hash from before TLS finalization.

## 6. Validate and deploy into the network

Run provider validation and a what-if preview before deployment. The template
currently uses registry username/password authentication; this example assumes
ACR admin credentials are enabled and permitted by your organization. If they
are not, adapt the template's registry authentication before proceeding.

Supply the registry password and actual TLS values through a temporary secure
parameter file rather than putting their contents in command-line arguments.
Use a protected deployment workstation and restrict access to its temporary
directory:

```powershell
# Run from infra. Do not print or retain the secure parameter file.
$ErrorActionPreference = "Stop"
$acrPwd = az acr credential show -n <acr-name> --query "passwords[0].value" -o tsv
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($acrPwd)) {
    throw "Registry credential retrieval failed."
}
$secureParamsPath = [System.IO.Path]::GetTempFileName()
try {
    $secureParams = @{
        parameters = @{
            registryPassword = @{ value = $acrPwd }
            tlsCert = @{ value = (Get-Content .\tls\cert.pem -Raw) }
            tlsKey = @{ value = (Get-Content .\tls\key.pem -Raw) }
        }
    } | ConvertTo-Json -Depth 5
    [System.IO.File]::WriteAllText($secureParamsPath, $secureParams)

    az deployment group validate -g <resource-group> --template-file .\template-vnet.json --parameters .\parameters-vnet.json "@$secureParamsPath"
    if ($LASTEXITCODE -ne 0) { throw "ARM validation failed." }
    az deployment group what-if -g <resource-group> --template-file .\template-vnet.json --parameters .\parameters-vnet.json "@$secureParamsPath"
    if ($LASTEXITCODE -ne 0) { throw "ARM what-if failed." }

    # Only proceed after reviewing and approving the what-if output.
    az deployment group create -g <resource-group> -n sandbox-private-deploy --confirm-with-what-if --template-file .\template-vnet.json --parameters .\parameters-vnet.json "@$secureParamsPath"
    if ($LASTEXITCODE -ne 0) { throw "ARM deployment failed." }
} finally {
    Remove-Item -LiteralPath $secureParamsPath -Force
}
```

Review the proposed changes; do not run `create` after a failed validation, and
approve any deletion/recreation or session-loss interruption before proceeding.
Secure parameters prevent normal ARM output/history from returning their values;
they do not remove the need to protect the deployment workstation, temporary
files, and logging. The temporary file contains plaintext credentials until
removed.

Retrieve the current IP, publish private DNS, and verify network reachability:

```powershell
az container show -g <resource-group> -n mcp-build-sandbox --query "{state:instanceView.state,ip:ipAddress.ip}" -o json
Test-NetConnection sandbox.corp.example -Port 8000
```

A TCP success is not a TLS or authorization test. Confirm trusted HTTPS, then
authenticated MCP initialization and a synthetic calculation before using real
data. Do not work around `SSL: WRONG_VERSION_NUMBER` by switching to HTTP.

## 7. Onboard people without distributing application credentials

### Current shared-agent model

The Agent Framework notebook demonstrates two identities with hardcoded names;
it does **not** implement a production user sign-in service.

For a real shared agent:

1. Authenticate users in your front-end with Entra. Add the necessary web/desktop
   authentication configuration, or a separate front-end registration if your
   architecture separates it from the daemon client.
2. Restrict access using your application's user/group authorization and
   organizational Conditional Access requirements.
3. Derive a stable user identifier from the validated sign-in context, never from
   a prompt or a client-supplied arbitrary header.
4. Keep the client's secret/certificate on the trusted service. Acquire app-only
   sandbox tokens there.
5. Set `X-User-Id` in trusted code and hold the user-to-session map server-side.
   Do not allow the model to choose user IDs or session IDs.
6. Return only permitted results to the user/model, and delete sessions according
   to your retention policy.

The sandbox's authenticated identity is the **application**, not an independently
authenticated human user. `X-User-Id` is an application assertion. This guards
against accidental routing mistakes but does not protect against a compromised
agent or hostile code inside the shared container.

### If the sandbox must cryptographically authenticate each user

Use a delegated design such as authorization-code sign-in plus On-Behalf-Of
(OBO) through a trusted middle tier. You need delegated API scopes, consent,
token acquisition, and explicit API permission checks. Do not give a daemon's
secret to each user or make one app registration per person.

The current notebooks implement **client credentials**, not this delegated flow.
Adding user delegation is separate application work; it does not fix the lack of
OS isolation between executions within the same container group.

### Notebook-only development access

Only trusted developers should have the notebook client's credentials.
Connect to the VPN, select the repository `.venv` kernel, configure
`examples\.env`, and run the cells in order. Never use this as the credential
distribution model for all end users.

For the Agent Framework notebook, Azure OpenAI authentication is separate:
configure the **resource endpoint** and model deployment, sign in with `az login`,
and give that local identity appropriate inference permissions. A Foundry project
URL is not interchangeable with the Azure OpenAI endpoint used by this client.

## Before production

Confirm user isolation requirements, verified TLS, controlled egress, caller
authorization, session cleanup, resource limits, logging/retention policy, key and
certificate renewal, DNS updates, trusted administration, and recovery procedures.
Choose dynamic sessions instead if their execution isolation meets the requirement
and confidential computing is not necessary.

## References

- [Confidential containers on ACI](https://learn.microsoft.com/azure/container-instances/container-instances-confidential-overview)
- [ACI virtual-network deployment](https://learn.microsoft.com/azure/container-instances/container-instances-vnet)
- [Entra client-credentials flow and assignment requirements](https://learn.microsoft.com/entra/identity-platform/v2-oauth2-client-creds-grant-flow)
- [Entra On-Behalf-Of flow](https://learn.microsoft.com/entra/identity-platform/v2-oauth2-on-behalf-of-flow)
- [Entra-authenticated Azure VPN Client](https://learn.microsoft.com/azure/vpn-gateway/point-to-site-entra-vpn-client)
- [Dynamic sessions security](https://learn.microsoft.com/azure/container-apps/sessions-usage)
