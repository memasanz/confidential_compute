# Deploy the pilot gateway/controller Container Apps (run after deploy-pool-infra.ps1 stages). Staged, idempotent,
# no secrets in the repo. Operational inputs/outputs live in the git-ignored .azure\pool-pilot\ directory:
#   pki\ (from make_pki.py), policy-base.json (from make_policy_base.py), apps.parameters.json (generated here).
#
#   .\deploy-pool-apps.ps1 -Stage ingress-dns [-WhatIfOnly]   # private zone for the env default domain
#   .\deploy-pool-apps.ps1 -Stage material    [-WhatIfOnly]   # controller-only Azure Files share + env storage
#   .\deploy-pool-apps.ps1 -Stage build                      # stage context + az acr build (new tag, prints digest)
#   .\deploy-pool-apps.ps1 -Stage apps -ImageDigest sha256:.. [-WhatIfOnly] [-DesiredReady 2 -MaxTotal 3]
# Pause/resume: -Stage apps -DesiredReady 0   (controller creates no new workers) or
#   az containerapp update -g rg-confidential-pool-control -n pool-controller --min-replicas 0 --max-replicas 0
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('ingress-dns', 'material', 'build', 'apps')][string]$Stage,
    [switch]$WhatIfOnly,
    [string]$ImageDigest,
    [int]$DesiredReady = 2,
    [int]$MaxTotal = 3,
    [int]$QueueWait = 600,
    [string]$Subscription = '7ee2b43a-eaea-4259-be7b-c8c220bfbcf9',
    [string]$ControlRg = 'rg-confidential-pool-control',
    [string]$WorkerRg = 'rg-confidential-pool',
    [string]$VnetRg = 'rg-fabric-foundry-eus2',
    [string]$VnetName = 'vnet-fabric-foundry',
    [string]$AcrName = 'acrconfmcpvisdzn',
    [string]$EnvName = 'cae-confidential-pool',
    [string]$WorkerImageDigest = 'sha256:7268e1e221b4c162bf3521992af063d08a17a9e6d0e4c8a74fcb14bc10a87ff7',
    [string]$SidecarImageDigest = 'sha256:b584057158c1f700edcdb0b3122628541da450acac48bd80512ee88c34f7649d',
    [string]$ApiAudience = 'cee796a9-d196-4e24-86f3-807f9e669625',
    [string]$ApiRole = 'Sandbox.Access',
    [string]$TenantId = '7b3b2559-0c78-44e7-8c57-8455603aca36'
)
$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$pilot = Join-Path $root '.azure\pool-pilot'
$here = $PSScriptRoot
az account set --subscription $Subscription
$vnetId = "/subscriptions/$Subscription/resourceGroups/$VnetRg/providers/Microsoft.Network/virtualNetworks/$VnetName"
$suffix = ($Subscription -replace '-', '').Substring(0, 6)
$acr = "$AcrName.azurecr.io"

function Invoke-Az { $r = & az @args; if ($LASTEXITCODE -ne 0) { throw "az $($args -join ' ') failed" }; $r }
function Deploy-Bicep($name, $file, $params) {
    $a = @('-g', $ControlRg, '-n', $name, '--template-file', (Join-Path $here "bicep\$file"), '--parameters') + $params
    Write-Host "== what-if $name"
    Invoke-Az deployment group what-if @a --no-pretty-print --query "changes[?changeType!='Ignore'].{t:resourceId,c:changeType}" -o table | Out-Host
    Invoke-Az deployment group validate @a --query properties.provisioningState -o tsv | Out-Host
    if ($WhatIfOnly) { return }
    Invoke-Az deployment group create @a -o json | ConvertFrom-Json
}
function Identity($n) { $i = Invoke-Az identity show -g $ControlRg -n $n --query '{id:id,c:clientId}' -o json | ConvertFrom-Json; $i.id = $i.id -replace '/resourcegroups/', '/resourceGroups/'; $i }

switch ($Stage) {
    'ingress-dns' {
        $e = Invoke-Az containerapp env show -g $ControlRg -n $EnvName --query '{d:properties.defaultDomain,ip:properties.staticIp}' -o json | ConvertFrom-Json
        $null = Deploy-Bicep 'pool-ingress-dns' 'ingress-dns.bicep' @("defaultDomain=$($e.d)", "staticIp=$($e.ip)", "vnetId=$vnetId")
    }
    'material' {
        $null = Deploy-Bicep 'pool-material' 'material.bicep' @("storageName=stpoolmat$suffix", "vnetId=$vnetId", "peSubnetId=$vnetId/subnets/snet-pe2")
    }
    'build' {
        $ctx = Join-Path $pilot 'ctx'
        Remove-Item -Recurse -Force $ctx -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Force "$ctx\pool-control", "$ctx\infra\pool", "$ctx\mcp-build-sandbox" | Out-Null
        Copy-Item "$root\pool-control\*.py", "$root\pool-control\requirements.txt", "$root\pool-control\Dockerfile" "$ctx\pool-control"
        Copy-Item "$here\finalize_policy.py", "$here\worker.example.json", "$here\smoke.py", (Join-Path $pilot 'policy-base.json') "$ctx\infra\pool"
        Copy-Item "$root\mcp-build-sandbox\pool_worker.py" "$ctx\mcp-build-sandbox"
        $tag = 'pool-control-' + (Get-Date -Format yyyyMMddHHmm)
        Push-Location $root
        Invoke-Az acr build -r $AcrName -t "pool-control:$tag" -f pool-control/Dockerfile $ctx --no-logs | Out-Null
        Pop-Location
        Write-Host "tag=$tag"
        Invoke-Az acr repository show -n $AcrName --image "pool-control:$tag" --query digest -o tsv
    }
    'apps' {
        if ($ImageDigest -notmatch '^sha256:[0-9a-f]{64}$') { throw 'Pass -ImageDigest sha256:<digest> from the build stage' }
        $gw = Identity 'id-pool-gateway'; $ctl = Identity 'id-pool-controller'; $pull = Identity 'id-pool-acrpull'
        $domain = Invoke-Az containerapp env show -g $ControlRg -n $EnvName --query properties.defaultDomain -o tsv
        $pki = Join-Path $pilot 'pki'
        $text = { param($n) (Get-Content (Join-Path $pki $n) -Raw) }
        $common = [ordered]@{
            POOL_IDLE = 1800; POOL_ABSOLUTE = 14400; POOL_QUEUE_WAIT = $QueueWait; POOL_QUEUE_SIZE = 20
            POOL_OPERATION = 300; POOL_CLAIM = 420; POOL_UPLOAD_BYTES = 1048576; POOL_RESULT_BYTES = 1048576
            POOL_PROVISIONING_AGE = 900; POOL_TERMINAL_RETENTION = 86400; POOL_SDK_TIMEOUT = 300; POOL_TABLE_TIMEOUT = 20
            POOL_MAX_TOTAL = $MaxTotal; POOL_DESIRED_READY = $DesiredReady
            POOL_TABLE_ENDPOINT = "https://stpoolstate$suffix.table.core.windows.net"; POOL_TABLE_NAME = 'poolstate'
            POOL_PARTITION = 'pool'; POOL_DNS_ZONE = 'workers.pool.internal'; POOL_SUBNET_CIDR = '192.168.6.0/24'
            POOL_MAA_ISSUER = 'https://sharedeus2.eus2.attest.azure.net'
            POOL_MINIMUM_SVN = '{"bootloader":10,"tee":0,"snpfw":23,"microcode":84,"guest":2,"uvm_guest":104}'
            POOL_ALLOW_SMT = 'true'
        }
        $gatewayEnv = [ordered]@{
            POOL_ENTRA_TENANT = $TenantId; POOL_ENTRA_AUDIENCE = $ApiAudience; POOL_ALLOWED_ROLES = $ApiRole
            POOL_BIND_HOST = '0.0.0.0'; POOL_BIND_PORT = 8000
        }
        $controllerEnv = [ordered]@{
            POOL_SUBSCRIPTION = $Subscription; POOL_RESOURCE_GROUP = $WorkerRg; POOL_POOL_ID = 'pilot'
            POOL_RESOURCE_PREFIX = 'pool'; POOL_LOCATION = 'eastus2'
            POOL_SUBNET_ID = "$vnetId/subnets/snet-pool-workers"; POOL_DNS_RESOURCE_GROUP = $ControlRg
            POOL_RECONCILE_SECONDS = 15; POOL_MATERIAL_FACTORY = 'material:environment'
            POOL_MATERIAL_ACL_CONFIRMED = 'controller-only'
            POOL_WORKER_IMAGE = "$acr/mcp-build-sandbox@$WorkerImageDigest"; POOL_SIDECAR_IMAGE = "$acr/aci/skr@$SidecarImageDigest"
            POOL_CERT_DAYS = 2; POOL_CONFCOM_TIMEOUT = 1200; POOL_AZ_EXECUTABLE = 'az'
            POOL_POLICY_BASE = '/app/infra/pool/policy-base.json'
            POOL_PULL_IDENTITY = $pull.id; POOL_REGISTRY_SERVER = $acr
            POOL_ALLOW_PULL_RUNTIME_IDENTITY = 'true'; POOL_PULL_ROLE_ATTESTED = 'acr-pull-only'
        }
        $p = [ordered]@{
            image = "$acr/pool-control@$ImageDigest"; registryServer = $acr; pullIdentityId = $pull.id
            gatewayIdentityId = $gw.id; gatewayClientId = $gw.c; controllerIdentityId = $ctl.id; controllerClientId = $ctl.c
            gatewayUrl = "https://pool-gateway.$domain/mcp"; common = $common; gatewayEnv = $gatewayEnv; controllerEnv = $controllerEnv
            serverCaCert = (& $text 'server-ca.pem'); serverCaKey = (& $text 'server-ca.key'); clientCaCert = (& $text 'client-ca.pem')
            gatewayClientCert = (& $text 'gateway-client.pem'); gatewayClientKey = (& $text 'gateway-client.key')
        }
        $doc = @{ '$schema' = 'https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#'; contentVersion = '1.0.0.0'
                  parameters = @{} }
        foreach ($k in $p.Keys) { $doc.parameters[$k] = @{ value = $p[$k] } }
        $file = Join-Path $pilot 'apps.parameters.json'
        $doc | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8 $file
        $r = Deploy-Bicep 'pool-apps' 'apps.bicep' @("@$file")
        if ($r) { Write-Host "gateway: https://$($r.properties.outputs.gatewayFqdn.value)/mcp" }
    }
}
