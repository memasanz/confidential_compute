# Deploy the confidential code sandbox INTO AN EXISTING VNET (private IP, no public IP).
#
# This variant injects the confidential container group into an existing virtual
# network. It creates only the pieces ACI needs (a delegated subnet, a NAT gateway
# for egress, and private endpoints for HSM + ACR) and reuses everything else.
#
# Requires: Azure CLI 2.44.1+, the confcom extension (0.30+), an existing VNet,
# an ACR, a managed HSM, and a user-assigned managed identity.
#
# Secrets (ACR password + in-enclave TLS cert/key) are passed as SECURE PARAMETERS,
# not Key Vault references, because governed subscriptions commonly force standard
# Key Vaults to private-only (policy KeyVault_PublicNetwork_Modify).

$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"; $env:PYTHONUTF8 = "1"

# ---- Edit these: target (existing) network ----
$ResourceGroup = "rg-confidential-mcp"      # RG for the new confidential resources
$Location      = "eastus2"                   # must support confidential ACI
$VnetRg        = "rg-fabric-foundry-eus2"    # RG that holds the EXISTING VNet
$VnetName      = "vnet-fabric-foundry"       # EXISTING VNet to deploy into
$AciSubnetName = "snet-aci"                  # delegated subnet to create/reuse
$AciSubnetCidr = "192.168.3.0/28"            # must be free in the VNet address space
$PeSubnetName  = "snet-pe"                    # EXISTING subnet used for private endpoints

# ---- Edit these: confidential resources ----
$Acr         = "myregistry"                  # ACR name (without .azurecr.io)
$Hsm         = "my-hsm"                       # managed HSM name (holds the SKR key)
$Identity    = "mi-mcp-sandbox"              # user-assigned managed identity name
$MaaEndpoint = "https://sharedeus2.eus2.attest.azure.net"
$ImageTag    = "mcp-build-sandbox:1.0"
$KeyName     = "mcp-auth-token"

# ---- Edit these: Entra auth (production) ----
$EntraTenantId = ""                           # e.g. tenant guid
$EntraAudience = ""                           # e.g. api://<app-id>
# ------------------------------------------------

$Image = "$Acr.azurecr.io/$ImageTag"

# 1. Build and push the image to ACR (ACR Tasks builds remotely).
az acr build --registry $Acr --image $ImageTag ..\mcp-build-sandbox

# 2. Delegated subnet for the confidential group (private IP only).
az network vnet subnet create -g $VnetRg --vnet-name $VnetName -n $AciSubnetName `
  --address-prefixes $AciSubnetCidr `
  --delegations Microsoft.ContainerInstance/containerGroups

# 3. NAT gateway + public IP for egress-only outbound (MAA has no private endpoint).
az network public-ip create -g $ResourceGroup -n "pip-aci-nat" --sku Standard --allocation-method Static
az network nat gateway create -g $ResourceGroup -n "natgw-aci" --public-ip-addresses "pip-aci-nat" --idle-timeout 10
$natId = az network nat gateway show -g $ResourceGroup -n "natgw-aci" --query id -o tsv
az network vnet subnet update -g $VnetRg --vnet-name $VnetName -n $AciSubnetName --nat-gateway $natId

# 4. Private endpoints for HSM + ACR (keep key release and image pulls private).
#    Assumes the private DNS zones are linked to the VNet (privatelink.azurecr.io
#    typically already exists in a Foundry network; the HSM zone is created here).
$hsmId = az keyvault show --hsm-name $Hsm --query id -o tsv
az network private-endpoint create -g $ResourceGroup -n "pe-hsm" `
  --vnet-name $VnetName --subnet $PeSubnetName --vnet-rg $VnetRg `
  --private-connection-resource-id $hsmId --group-id "managedhsm" `
  --connection-name "pe-hsm-conn"

$acrId = az acr show -n $Acr --query id -o tsv
az network private-endpoint create -g $ResourceGroup -n "pe-acr" `
  --vnet-name $VnetName --subnet $PeSubnetName --vnet-rg $VnetRg `
  --private-connection-resource-id $acrId --group-id "registry" `
  --connection-name "pe-acr-conn"

# (Ensure privatelink.managedhsm.azure.net and privatelink.azurecr.io private DNS
#  zones exist and are linked to the VNet, with A records / zone groups for the PEs.)

# 5. Attestation-gated bearer key: exportable managed-HSM key whose release policy
#    only lets a genuine SEV-SNP enclave (via MAA) export it (Secure Key Release).
$identityPrincipal = az identity show -g $ResourceGroup -n $Identity --query principalId -o tsv
az keyvault role assignment create --hsm-name $Hsm --role "Managed HSM Crypto User" `
  --assignee $identityPrincipal --scope "/keys/$KeyName"

# The release policy binds the key to MAA claims (the CCE policy hash). Put the hash
# from step 6 into skr-release-policy.json BEFORE creating the key.
az keyvault key create --hsm-name $Hsm --name $KeyName --kty RSA --size 2048 `
  --exportable true --policy .\skr-release-policy.json

# 6. Generate the CCE policy (written into template-vnet.json's ccePolicy field).
az confcom acipolicygen -a .\template-vnet.json -p .\parameters-vnet.json

# 7. Deploy the confidential container group into the existing subnet.
$miId     = az identity show -g $ResourceGroup -n $Identity --query id -o tsv
$subnetId = az network vnet subnet show -g $VnetRg --vnet-name $VnetName -n $AciSubnetName --query id -o tsv
$acrPwd   = az acr credential show -n $Acr --query "passwords[0].value" -o tsv
# TLS: supply real PEM cert/key files for your private DNS name.
$tlsCert  = Get-Content .\tls\cert.pem -Raw
$tlsKey   = Get-Content .\tls\key.pem -Raw

az deployment group create `
  --resource-group $ResourceGroup `
  --template-file .\template-vnet.json `
  --parameters .\parameters-vnet.json `
  --parameters image=$Image registryServer="$Acr.azurecr.io" registryUsername=$Acr `
               registryPassword=$acrPwd `
               akvEndpoint="https://$Hsm.managedhsm.azure.net" maaEndpoint=$MaaEndpoint `
               subnetId=$subnetId managedIdentityResourceId=$miId `
               entraTenantId=$EntraTenantId entraAudience=$EntraAudience `
               tlsCert=$tlsCert tlsKey=$tlsKey

# 8. Read the (private) MCP endpoint. Reachable from inside the VNet or over VPN.
az deployment group show -g $ResourceGroup -n template-vnet --query properties.outputs.mcpEndpoint.value -o tsv
