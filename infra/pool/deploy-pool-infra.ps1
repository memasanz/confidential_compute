# Shared infrastructure for the confidential per-session worker pool. Idempotent, staged, no secrets.
# Gateway/controller apps are NOT deployed here. Generated outputs go to infra\pool\out\ (git-ignored).
#
# Supply explicit subscription, region and stage-specific existing-resource/network inputs.
# -WhatIfOnly previews the selected stage without deploying.
# Stages in order: preflight, network, identity, state, dns, rbac, environment
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('preflight', 'network', 'identity', 'state', 'dns', 'rbac', 'environment')]
    [string]$Stage,
    [switch]$WhatIfOnly,
    [string]$Subscription,
    [string]$Location,
    [string]$VnetRg,
    [string]$VnetName,
    [string]$PeSubnetName,
    [string]$ControlRg = 'rg-confidential-pool-control',
    [string]$WorkerRg = 'rg-confidential-pool',
    [string]$ControlSubnetName = 'snet-pool-control',
    [string]$ControlCidr,
    [string]$WorkerSubnetName = 'snet-pool-workers',
    [string]$WorkerCidr,
    [string]$VnetCidr,
    [string]$WorkerPort = '8000',
    [string]$AcrRg,
    [string]$AcrName,
    [string]$DnsResolverIp,
    [string]$WorkerZone = 'workers.pool.internal',
    [string]$NatGatewayId,
    [bool]$AttachNat = $false,
    [bool]$AllowImagePullEgress = $false,
    [string]$LogWorkspaceName,
    [string]$LogWorkspaceRg,
    [string]$EnvName = 'cae-confidential-pool'
)
$ErrorActionPreference = 'Stop'

function Require-Inputs([string[]]$Names) {
    foreach ($name in $Names) {
        if ([string]::IsNullOrWhiteSpace((Get-Variable -Name $name -ValueOnly))) {
            throw "Stage '$Stage' requires explicit -$name"
        }
    }
}

function Assert-Cidr([string]$Name) {
    $value = Get-Variable -Name $Name -ValueOnly
    $parts = $value.Split('/')
    $ip = $null
    if ($parts.Count -ne 2 -or $parts[0] -notmatch '^\d+\.\d+\.\d+\.\d+$' -or
        -not [System.Net.IPAddress]::TryParse($parts[0], [ref]$ip) -or
        $parts[1] -notmatch '^(?:[0-9]|[12][0-9]|3[0-2])$') {
        throw "-$Name must be an IPv4 network CIDR"
    }
    $bytes = $ip.GetAddressBytes()
    $address = [uint64]$bytes[0] * 16777216 + [uint64]$bytes[1] * 65536 + [uint64]$bytes[2] * 256 + $bytes[3]
    if ($address % [math]::Pow(2, 32 - [int]$parts[1]) -ne 0) {
        throw "-$Name must use a network address, not host bits"
    }
}

Require-Inputs @('Subscription')
if ($Subscription -notmatch '^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$') {
    throw '-Subscription must be a subscription GUID'
}
if ($Stage -in @('preflight', 'network', 'identity', 'state', 'environment')) { Require-Inputs @('Location') }
if ($Stage -in @('preflight', 'network', 'state', 'dns', 'rbac', 'environment')) { Require-Inputs @('VnetRg', 'VnetName') }
if ($Stage -in @('preflight', 'network')) {
    Require-Inputs @('ControlCidr', 'WorkerCidr')
    Assert-Cidr 'ControlCidr'
    Assert-Cidr 'WorkerCidr'
}
if ($Stage -eq 'network') {
    Require-Inputs @('VnetCidr', 'DnsResolverIp')
    Assert-Cidr 'VnetCidr'
    $resolver = $null
    if ($DnsResolverIp -notmatch '^\d+\.\d+\.\d+\.\d+$' -or
        -not [System.Net.IPAddress]::TryParse($DnsResolverIp, [ref]$resolver)) {
        throw '-DnsResolverIp must be an IPv4 address'
    }
    if ($AttachNat) {
        Require-Inputs @('NatGatewayId')
        if ($NatGatewayId -notmatch '^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft.Network/natGateways/[^/]+$') {
            throw '-NatGatewayId must be a NAT gateway resource ID'
        }
    }
}
if ($Stage -eq 'state') { Require-Inputs @('PeSubnetName') }
if ($Stage -eq 'rbac') { Require-Inputs @('AcrRg', 'AcrName') }
if ($Stage -eq 'environment') { Require-Inputs @('LogWorkspaceName', 'LogWorkspaceRg') }

$here = $PSScriptRoot
$out = Join-Path $here 'out'
New-Item -ItemType Directory -Force $out | Out-Null

$vnetId = "/subscriptions/$Subscription/resourceGroups/$VnetRg/providers/Microsoft.Network/virtualNetworks/$VnetName"
$workerSubnetId = "$vnetId/subnets/$WorkerSubnetName"
$controlSubnetId = "$vnetId/subnets/$ControlSubnetName"
$peSubnetId = "$vnetId/subnets/$PeSubnetName"
$suffix = (($Subscription -replace '-', '').Substring(0, 6))
$storageName = "stpoolstate$suffix"
$tableName = 'poolstate'
$tableDataContributor = "/subscriptions/$Subscription/providers/Microsoft.Authorization/roleDefinitions/0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3"

function Invoke-Az {
    $r = & az @args
    if ($LASTEXITCODE -ne 0) { throw "az $($args -join ' ') failed" }
    $r
}

Invoke-Az account set --subscription $Subscription

function Ensure-Rg($name) {
    if ((az group exists -n $name) -ne 'true') {
        if ($WhatIfOnly) { Write-Host "[what-if] would create resource group $name"; return }
        Invoke-Az group create -n $name -l $Location --tags purpose=confidential-pool | Out-Null
    }
}

# what-if first; deploy unless -WhatIfOnly. Skips what-if when the target RG does not exist yet.
function Deploy-Bicep($rg, $name, $file, $params) {
    $args2 = @('-g', $rg, '-n', $name, '--template-file', (Join-Path $here "bicep\$file"))
    if ($params.Count) { $args2 += @('--parameters') + $params }
    if ((az group exists -n $rg) -eq 'true') {
        Write-Host "== what-if $name"
        Invoke-Az deployment group what-if @args2 | Out-Host
    } else { Write-Host "[what-if] RG $rg not created yet; skipping what-if for $name" }
    if ($WhatIfOnly) { return $null }
    Invoke-Az deployment group validate @args2 | Out-Null
    Invoke-Az deployment group create @args2 -o json | ConvertFrom-Json
}

function Ensure-Role($file, $replacements) {
    $text = Get-Content (Join-Path $here "roles\$file") -Raw
    foreach ($k in $replacements.Keys) { $text = $text.Replace("{$k}", $replacements[$k]) }
    $def = $text | ConvertFrom-Json
    $path = Join-Path $out $file
    $text | Set-Content -Encoding utf8 $path
    $existing = az role definition list --name $def.Name --custom-role-only true --query '[0].id' -o tsv
    if ($WhatIfOnly) { Write-Host "[what-if] role '$($def.Name)' exists=$([bool]$existing)"; return $existing }
    if ($existing) { Invoke-Az role definition update --role-definition $path | Out-Null }
    else { Invoke-Az role definition create --role-definition $path | Out-Null }
    az role definition list --name $def.Name --custom-role-only true --query '[0].id' -o tsv
}

switch ($Stage) {
    'preflight' {
        Write-Host '== subnet overlap'
        $subnets = Invoke-Az network vnet subnet list -g $VnetRg --vnet-name $VnetName --query '[].{n:name,p:addressPrefix}' -o json | ConvertFrom-Json
        $subnets | Format-Table
        foreach ($c in $ControlCidr, $WorkerCidr) {
            $clash = python -c "import ipaddress,sys,json;c=ipaddress.ip_network(sys.argv[1]);print(json.dumps([s['n'] for s in json.loads(sys.argv[2]) if s['n'] not in sys.argv[3:] and ipaddress.ip_network(s['p']).overlaps(c)]))" $c ($subnets | ConvertTo-Json -Compress) $ControlSubnetName $WorkerSubnetName
            Write-Host "$c overlaps: $clash"
        }
        Write-Host '== policy assignments (subscription)'
        Invoke-Az policy assignment list --query '[].{n:displayName,mode:enforcementMode}' -o table
        Write-Host '== confidential ACI quota'
        $u = (Invoke-Az rest --method get --url "https://management.azure.com/subscriptions/$Subscription/providers/Microsoft.ContainerInstance/locations/$Location/usages?api-version=2023-05-01" -o json | ConvertFrom-Json).value
        $u | Where-Object { $_.name.value -like 'Confidential*' } | ForEach-Object { Write-Host "$($_.name.value): $($_.currentValue)/$($_.limit)" }
    }
    'network' {
        Ensure-Rg $ControlRg
        $nsg = Deploy-Bicep $ControlRg 'pool-nsg' 'nsg.bicep' @("location=$Location", "controlCidr=$ControlCidr", "workerCidr=$WorkerCidr", "vnetCidr=$VnetCidr", "workerPort=$WorkerPort", "dnsResolverIp=$DnsResolverIp", "allowImagePullEgress=$($AllowImagePullEgress.ToString().ToLower())")
        $nsgId = "/subscriptions/$Subscription/resourceGroups/$ControlRg/providers/Microsoft.Network/networkSecurityGroups/nsg-pool-workers"
        if ($WhatIfOnly) {
            Write-Host "[what-if] would create subnet $ControlSubnetName $ControlCidr (delegation Microsoft.App/environments)"
            Write-Host "[what-if] would create subnet $WorkerSubnetName $WorkerCidr (delegation Microsoft.ContainerInstance/containerGroups, NSG nsg-pool-workers, NAT=$AttachNat)"
            return
        }
        if (-not (az network vnet subnet show -g $VnetRg --vnet-name $VnetName -n $ControlSubnetName 2>$null)) {
            Invoke-Az network vnet subnet create -g $VnetRg --vnet-name $VnetName -n $ControlSubnetName --address-prefixes $ControlCidr --delegations Microsoft.App/environments -o none
        }
        if (-not (az network vnet subnet show -g $VnetRg --vnet-name $VnetName -n $WorkerSubnetName 2>$null)) {
            $sa = @('network', 'vnet', 'subnet', 'create', '-g', $VnetRg, '--vnet-name', $VnetName, '-n', $WorkerSubnetName, '--address-prefixes', $WorkerCidr, '--delegations', 'Microsoft.ContainerInstance/containerGroups', '--network-security-group', $nsgId)
            if ($AttachNat) { $sa += @('--nat-gateway', $NatGatewayId) }
            Invoke-Az @sa -o none
        }
        Invoke-Az network vnet subnet show -g $VnetRg --vnet-name $VnetName -n $WorkerSubnetName --query '{p:addressPrefix,d:delegations[0].serviceName,nsg:networkSecurityGroup.id,nat:natGateway.id}' -o json
    }
    'identity' {
        Ensure-Rg $ControlRg
        Ensure-Rg $WorkerRg
        $null = Deploy-Bicep $ControlRg 'pool-identities' 'identities.bicep' @("location=$Location")
    }
    'state' {
        Ensure-Rg $ControlRg
        $null = Deploy-Bicep $ControlRg 'pool-state' 'state.bicep' @("location=$Location", "storageName=$storageName", "tableName=$tableName", "vnetId=$vnetId", "peSubnetId=$peSubnetId")
    }
    'dns' {
        Ensure-Rg $ControlRg
        $null = Deploy-Bicep $ControlRg 'pool-dns' 'dns.bicep' @("zoneName=$WorkerZone", "vnetId=$vnetId")
    }
    'rbac' {
        $rep = @{ SUBSCRIPTION = $Subscription; WORKER_RG = $WorkerRg; VNET_RG = $VnetRg; CONTROL_RG = $ControlRg }
        $aciRole = Ensure-Role 'aci-worker-manager.json' $rep
        $joinRole = Ensure-Role 'subnet-join.json' $rep
        $dnsRole = Ensure-Role 'dns-a-writer.json' $rep
        if ($WhatIfOnly -and -not ($aciRole -and $joinRole -and $dnsRole)) { Write-Host '[what-if] custom roles not created yet; assignments are previewed after they exist'; return }
        $ids = Invoke-Az identity list -g $ControlRg --query '[].{n:name,p:principalId}' -o json | ConvertFrom-Json
        $gw = ($ids | Where-Object n -eq 'id-pool-gateway').p
        $ctl = ($ids | Where-Object n -eq 'id-pool-controller').p
        $pull = ($ids | Where-Object n -eq 'id-pool-acrpull').p
        $mio = "/subscriptions/$Subscription/providers/Microsoft.Authorization/roleDefinitions/f1a07417-d97a-45cb-824c-7a7467783830"
        $acrPull = "/subscriptions/$Subscription/providers/Microsoft.Authorization/roleDefinitions/7f951dda-4ed3-4680-a7ca-43fe172d538d"
        $null = Deploy-Bicep $ControlRg 'pool-rbac-control' 'rbac-control.bicep' @("storageName=$storageName", "tableName=$tableName", "zoneName=$WorkerZone", "gatewayPrincipalId=$gw", "controllerPrincipalId=$ctl", "tableDataContributorRoleId=$tableDataContributor", "dnsWriterRoleId=$dnsRole", "managedIdentityOperatorRoleId=$mio")
        $null = Deploy-Bicep $AcrRg 'pool-rbac-acr' 'rbac-acr.bicep' @("registryName=$AcrName", "principalId=$pull", "acrPullRoleId=$acrPull")
        $null = Deploy-Bicep $WorkerRg 'pool-rbac-workers' 'rbac-rg.bicep' @("principalId=$ctl", "roleDefinitionId=$aciRole")
        $null = Deploy-Bicep $VnetRg 'pool-rbac-subnet' 'rbac-subnet.bicep' @("vnetName=$VnetName", "subnetName=$WorkerSubnetName", "principalId=$ctl", "roleDefinitionId=$joinRole")
    }
    'environment' {
        Ensure-Rg $ControlRg
        $r = Deploy-Bicep $ControlRg 'pool-environment' 'environment.bicep' @("location=$Location", "name=$EnvName", "infraSubnetId=$controlSubnetId", "logWorkspaceName=$LogWorkspaceName", "logWorkspaceRg=$LogWorkspaceRg")
        if ($r) { $r.properties.outputs | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 (Join-Path $out 'environment.json') }
    }
}
