# Build, push, generate the CCE policy, and deploy the confidential code sandbox.
# Requires: Azure CLI 2.44.1+, the confcom extension (0.30+), an ACR, a managed HSM,
# and a user-assigned managed identity.

$ErrorActionPreference = "Stop"

# ---- Edit these ----
$ResourceGroup = "rg-confidential-mcp"
$Location      = "northeurope"          # must support confidential ACI
$Acr           = "myregistry"           # ACR name (without .azurecr.io)
$KeyVault      = "my-keyvault"          # standard vault, for the ACR password + TLS
$Hsm           = "my-hsm"               # managed HSM name (holds the SKR key)
$Identity      = "mi-mcp-sandbox"       # user-assigned managed identity name
$MaaEndpoint   = "https://sharedeus.eus.attest.azure.net"
$ImageTag      = "mcp-build-sandbox:1.0"
# --------------------

$Image = "$Acr.azurecr.io/$ImageTag"

# 1. Build and push the image to ACR (ACR Tasks builds remotely).
az acr build --registry $Acr --image $ImageTag ..\mcp-build-sandbox

# 2. Standard-vault secrets: ACR password + in-enclave TLS cert/key.
$acrPassword = az acr credential show -n $Acr --query "passwords[0].value" -o tsv
az keyvault secret set --vault-name $KeyVault --name acr-password --value $acrPassword | Out-Null
# Store a PEM cert + key as secrets mcp-tls-cert / mcp-tls-key (use your real DNS
# name in production). TLS terminates inside the enclave.

# 3. Attestation-gated bearer token: an exportable managed-HSM key whose release
#    policy only lets a genuine SEV-SNP enclave (via MAA) export it (Secure Key Release).
$identityPrincipal = az identity show -g $ResourceGroup -n $Identity --query principalId -o tsv
az keyvault role assignment create --hsm-name $Hsm --role "Managed HSM Crypto User" `
  --assignee $identityPrincipal --scope "/keys/mcp-auth-token"

# The release policy binds the key to MAA claims (e.g. the CCE policy hash). See
# https://learn.microsoft.com/azure/confidential-computing/skr-policy-examples
az keyvault key create --hsm-name $Hsm --name mcp-auth-token --kty RSA --size 2048 `
  --exportable true --policy .\skr-release-policy.json

# 4. Generate the CCE policy (written into template.json's ccePolicy field).
az confcom acipolicygen -a .\template.json

# 5. Deploy the confidential container group.
az deployment group create `
  --resource-group $ResourceGroup `
  --template-file .\template.json `
  --parameters .\parameters.json `
  --parameters image=$Image registryServer="$Acr.azurecr.io" registryUsername=$Acr `
               akvEndpoint="https://$Hsm.managedhsm.azure.net" maaEndpoint=$MaaEndpoint

# 6. Read the MCP endpoint from the deployment output.
az deployment group show -g $ResourceGroup -n template --query properties.outputs.mcpEndpoint.value -o tsv
