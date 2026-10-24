# Deploy gateway/controller Container Apps (run after deploy-pool-infra.ps1 stages). Staged, idempotent,
# no secrets in the repo. Operational inputs/outputs live in the git-ignored .azure\pool\ directory:
#   pki\ (from make_pki.py), policy-base.json (from make_policy_base.py), apps.parameters.json (generated here).
#
# Supply explicit subscription and stage-specific existing-resource inputs.
# The apps stage also requires digest pins, approved SVN minima and explicit private-pull/ACL approval.
# Pause/resume: -Stage apps -DesiredReady 0   (controller creates no new workers) or
#   az containerapp update -g rg-confidential-pool-control -n pool-controller --min-replicas 0 --max-replicas 0
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('ingress-dns', 'build', 'apps')][string]$Stage,
    [switch]$WhatIfOnly,
    [string]$ImageDigest,
    [ValidateRange(0, 1000)][int]$DesiredReady = 0,
    [ValidateRange(1, 1000)][int]$MaxTotal = 3,
    [ValidateRange(1, 86400)][int]$QueueWait = 600,
    [string]$Subscription,
    [string]$ControlRg = 'rg-confidential-pool-control',
    [string]$WorkerRg = 'rg-confidential-pool',
    [string]$VnetRg,
    [string]$VnetName,
    [string]$AcrName,
    [string]$EnvName = 'cae-confidential-pool',
    [string]$WorkerImageDigest,
    [string]$SidecarImageDigest,
    [string]$ApiAudience,
    [string]$ApiRole = 'Sandbox.Access',
    [string]$TenantId,
    [string]$Location,
    [string]$WorkerSubnetName = 'snet-pool-workers',
    [string]$WorkerCidr,
    [string]$WorkerZone = 'workers.pool.internal',
    [string]$MaaIssuer,
    [string]$MinimumSvn,
    [bool]$AllowSmt = $false,
    [bool]$AllowPullRuntimeIdentity = $false,
    [string]$PullRoleAttested,
    [string]$MaterialAclConfirmed,
    [string]$PoolId = 'pool',
    [string]$ResourcePrefix = 'pool',
    [string]$OperationalDirectory = '.azure\pool'
)
$ErrorActionPreference = 'Stop'

function Require-Inputs([string[]]$Names) {
    foreach ($name in $Names) {
        if ([string]::IsNullOrWhiteSpace((Get-Variable -Name $name -ValueOnly))) {
            throw "Stage '$Stage' requires explicit -$name"
        }
    }
}

Require-Inputs @('Subscription')
if ($Subscription -notmatch '^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$') {
    throw '-Subscription must be a subscription GUID'
}
if ($Stage -in @('ingress-dns', 'apps')) { Require-Inputs @('VnetRg', 'VnetName') }
if ($Stage -in @('build', 'apps')) { Require-Inputs @('AcrName') }
if ($Stage -eq 'apps') { Require-Inputs @('Location') }
if ($Stage -eq 'build' -and $WhatIfOnly) {
    throw 'The build stage has no what-if mode; omit -WhatIfOnly to explicitly request an image build'
}
if ($Stage -eq 'apps') {
    Require-Inputs @('TenantId', 'ApiAudience', 'ImageDigest', 'WorkerImageDigest', 'SidecarImageDigest',
        'WorkerSubnetName', 'WorkerCidr', 'MaaIssuer', 'MinimumSvn', 'PullRoleAttested', 'MaterialAclConfirmed')
    if ($TenantId -notmatch '^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$') {
        throw '-TenantId must be a tenant GUID'
    }
    foreach ($name in @('ImageDigest', 'WorkerImageDigest', 'SidecarImageDigest')) {
        if ((Get-Variable -Name $name -ValueOnly) -cnotmatch '^sha256:[0-9a-f]{64}$') {
            throw "-$name must be sha256:<64 lowercase hex characters>"
        }
    }
    if ($DesiredReady -gt $MaxTotal) { throw '-DesiredReady must not exceed -MaxTotal' }
    $parts = $WorkerCidr.Split('/')
    $ip = $null
    if ($parts.Count -ne 2 -or $parts[0] -notmatch '^\d+\.\d+\.\d+\.\d+$' -or
        -not [System.Net.IPAddress]::TryParse($parts[0], [ref]$ip) -or
        $parts[1] -notmatch '^(?:[0-9]|[12][0-9]|3[0-2])$') {
        throw '-WorkerCidr must be an IPv4 network CIDR'
    }
    $bytes = $ip.GetAddressBytes()
    $address = [uint64]$bytes[0] * 16777216 + [uint64]$bytes[1] * 65536 + [uint64]$bytes[2] * 256 + $bytes[3]
    if ($address % [math]::Pow(2, 32 - [int]$parts[1]) -ne 0) {
        throw '-WorkerCidr must use a network address, not host bits'
    }
    $issuer = $null
    if (-not [uri]::TryCreate($MaaIssuer, [System.UriKind]::Absolute, [ref]$issuer) -or
        $issuer.Scheme -ne 'https' -or -not $issuer.IsDefaultPort -or $issuer.UserInfo -or
        $issuer.AbsolutePath -ne '/' -or $issuer.Query -or $issuer.Fragment) {
        throw '-MaaIssuer must be an HTTPS issuer origin (no path, credentials, query or fragment)'
    }
    $MaaIssuer = $MaaIssuer.TrimEnd('/')
    try { $svn = ConvertFrom-Json -InputObject $MinimumSvn }
    catch { throw '-MinimumSvn must be an approved JSON object of non-negative integer SVN minima' }
    $required = @('bootloader', 'tee', 'snpfw', 'microcode', 'guest', 'uvm_guest')
    if ($svn -isnot [pscustomobject] -or @($svn.PSObject.Properties).Count -ne $required.Count) {
        throw '-MinimumSvn must contain exactly bootloader, tee, snpfw, microcode, guest, uvm_guest'
    }
    foreach ($name in $required) {
        if ($svn.PSObject.Properties.Name -cnotcontains $name -or
            ($svn.$name -isnot [int] -and $svn.$name -isnot [long]) -or $svn.$name -lt 0) {
            throw "-MinimumSvn requires a non-negative integer for $name"
        }
    }
    $MinimumSvn = ConvertTo-Json -InputObject $svn -Compress
    if (-not $AllowPullRuntimeIdentity -or $PullRoleAttested -cne 'acr-pull-only') {
        throw 'Private registry workers require -AllowPullRuntimeIdentity $true -PullRoleAttested acr-pull-only; approve the residual runtime token risk and verify effective RBAC first'
    }
    if ($MaterialAclConfirmed -cne 'controller-only') {
        throw 'Explicit -MaterialAclConfirmed controller-only approval is required'
    }
}

$root = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$operational = if ([System.IO.Path]::IsPathRooted($OperationalDirectory)) { $OperationalDirectory } else { Join-Path $root $OperationalDirectory }
$here = $PSScriptRoot
if ($Stage -eq 'apps') {
    foreach ($name in @('client-ca.pem', 'gateway-client.pem', 'gateway-client.key')) {
        if (-not (Test-Path -LiteralPath (Join-Path $operational "pki\$name") -PathType Leaf)) {
            throw "Missing operational input pki\$name in -OperationalDirectory"
        }
    }
}
if ($Stage -eq 'build' -and -not (Test-Path -LiteralPath (Join-Path $operational 'policy-base.json') -PathType Leaf)) {
    throw 'Missing approved policy-base.json in -OperationalDirectory; generate/review it for the pinned worker/sidecar images before building'
}
$vnetId = "/subscriptions/$Subscription/resourceGroups/$VnetRg/providers/Microsoft.Network/virtualNetworks/$VnetName"
$suffix = ($Subscription -replace '-', '').Substring(0, 6)
$acr = "$AcrName.azurecr.io"

function Invoke-Az { $r = & az @args; if ($LASTEXITCODE -ne 0) { throw "az $($args -join ' ') failed" }; $r }
Invoke-Az account set --subscription $Subscription
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
    'build' {
        $ctx = Join-Path $operational 'ctx'
        Remove-Item -Recurse -Force $ctx -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Force "$ctx\pool-control", "$ctx\infra\pool", "$ctx\mcp-build-sandbox" | Out-Null
        Copy-Item "$root\pool-control\*.py", "$root\pool-control\requirements.txt", "$root\pool-control\Dockerfile" "$ctx\pool-control"
        Copy-Item "$here\finalize_policy.py", "$here\worker.example.json", "$here\smoke.py", "$here\orphan_cleanup.py", (Join-Path $operational 'policy-base.json') "$ctx\infra\pool"
        Copy-Item "$root\mcp-build-sandbox\pool_worker.py" "$ctx\mcp-build-sandbox"
        $tag = 'pool-control-' + (Get-Date -Format yyyyMMddHHmm)
        Push-Location $root
        Invoke-Az acr build -r $AcrName -t "pool-control:$tag" -f pool-control/Dockerfile $ctx --no-logs | Out-Null
        Pop-Location
        Write-Host "tag=$tag"
        Invoke-Az acr repository show -n $AcrName --image "pool-control:$tag" --query digest -o tsv
    }
    'apps' {
        $gw = Identity 'id-pool-gateway'; $ctl = Identity 'id-pool-controller'; $pull = Identity 'id-pool-acrpull'
        $domain = Invoke-Az containerapp env show -g $ControlRg -n $EnvName --query properties.defaultDomain -o tsv
        $pki = Join-Path $operational 'pki'
        $text = { param($n) (Get-Content (Join-Path $pki $n) -Raw) }
        $common = [ordered]@{
            POOL_IDLE = 1800; POOL_ABSOLUTE = 14400; POOL_QUEUE_WAIT = $QueueWait; POOL_QUEUE_SIZE = 20
            POOL_OPERATION = 300; POOL_CLAIM = 420; POOL_UPLOAD_BYTES = 1048576; POOL_RESULT_BYTES = 1048576
            POOL_PROVISIONING_AGE = 900; POOL_TERMINAL_RETENTION = 86400; POOL_SDK_TIMEOUT = 300; POOL_TABLE_TIMEOUT = 20
            POOL_MAX_TOTAL = $MaxTotal; POOL_DESIRED_READY = $DesiredReady
            POOL_TABLE_ENDPOINT = "https://stpoolstate$suffix.table.core.windows.net"; POOL_TABLE_NAME = 'poolstate'
            POOL_PARTITION = 'pool'; POOL_DNS_ZONE = $WorkerZone; POOL_SUBNET_CIDR = $WorkerCidr
            POOL_MAA_ISSUER = $MaaIssuer
            POOL_MINIMUM_SVN = $MinimumSvn
            POOL_ALLOW_SMT = $AllowSmt.ToString().ToLowerInvariant()
        }
        $gatewayEnv = [ordered]@{
            POOL_ENTRA_TENANT = $TenantId; POOL_ENTRA_AUDIENCE = $ApiAudience; POOL_ALLOWED_ROLES = $ApiRole
            POOL_BIND_HOST = '0.0.0.0'; POOL_BIND_PORT = 8000
        }
        $controllerEnv = [ordered]@{
            POOL_SUBSCRIPTION = $Subscription; POOL_RESOURCE_GROUP = $WorkerRg; POOL_POOL_ID = $PoolId
            POOL_RESOURCE_PREFIX = $ResourcePrefix; POOL_LOCATION = $Location
            POOL_SUBNET_ID = "$vnetId/subnets/$WorkerSubnetName"; POOL_DNS_RESOURCE_GROUP = $ControlRg
            POOL_RECONCILE_SECONDS = 15; POOL_MATERIAL_FACTORY = 'material:environment'
            POOL_MATERIAL_ACL_CONFIRMED = $MaterialAclConfirmed
            POOL_WORKER_IMAGE = "$acr/mcp-build-sandbox@$WorkerImageDigest"; POOL_SIDECAR_IMAGE = "$acr/aci/skr@$SidecarImageDigest"
            POOL_CONFCOM_TIMEOUT = 1200; POOL_AZ_EXECUTABLE = 'az'
            POOL_POLICY_BASE = '/app/infra/pool/policy-base.json'
            POOL_PULL_IDENTITY = $pull.id; POOL_REGISTRY_SERVER = $acr
            POOL_ALLOW_PULL_RUNTIME_IDENTITY = $AllowPullRuntimeIdentity.ToString().ToLowerInvariant(); POOL_PULL_ROLE_ATTESTED = $PullRoleAttested
        }
        $p = [ordered]@{
            location = $Location; environmentName = $EnvName
            image = "$acr/pool-control@$ImageDigest"; registryServer = $acr; pullIdentityId = $pull.id
            gatewayIdentityId = $gw.id; gatewayClientId = $gw.c; controllerIdentityId = $ctl.id; controllerClientId = $ctl.c
            gatewayUrl = "https://pool-gateway.$domain/mcp"; common = $common; gatewayEnv = $gatewayEnv; controllerEnv = $controllerEnv
            clientCaCert = (& $text 'client-ca.pem')
            gatewayClientCert = (& $text 'gateway-client.pem'); gatewayClientKey = (& $text 'gateway-client.key')
        }
        $doc = @{ '$schema' = 'https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#'; contentVersion = '1.0.0.0'
                  parameters = @{} }
        foreach ($k in $p.Keys) { $doc.parameters[$k] = @{ value = $p[$k] } }
        $file = Join-Path $operational 'apps.parameters.json'
        $doc | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8 $file
        $r = Deploy-Bicep 'pool-apps' 'apps.bicep' @("@$file")
        if ($r) { Write-Host "gateway: https://$($r.properties.outputs.gatewayFqdn.value)/mcp" }
    }
}
