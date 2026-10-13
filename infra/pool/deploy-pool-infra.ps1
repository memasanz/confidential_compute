# Shared infrastructure for the confidential per-session worker pool. Idempotent, staged, no secrets.
# Gateway/controller apps are NOT deployed here. Generated outputs go to infra\pool\out\ (git-ignored).
#
#   .\deploy-pool-infra.ps1 -Stage preflight
#   .\deploy-pool-infra.ps1 -Stage network -WhatIfOnly     # what-if only
#   .\deploy-pool-infra.ps1 -Stage network                 # what-if, then deploy
# Stages in order: preflight, network, identity, state, dns, rbac, environment
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('preflight', 'network', 'identity', 'state', 'dns', 'rbac', 'environment')]
    [string]$Stage,
    [switch]$WhatIfOnly,
    [string]$Subscription = '7ee2b43a-eaea-4259-be7b-c8c220bfbcf9',
    [string]$Location = 'eastus2',
    [string]$VnetRg = 'rg-fabric-foundry-eus2',
    [string]$VnetName = 'vnet-fabric-foundry',
    [string]$PeSubnetName = 'snet-pe2',
    [string]$ControlRg = 'rg-confidential-pool-control',
    [string]$WorkerRg = 'rg-confidential-pool',
    [string]$ControlSubnetName = 'snet-pool-control',
    [string]$ControlCidr = '192.168.5.0/27',
    [string]$WorkerSubnetName = 'snet-pool-workers',
    [string]$WorkerCidr = '192.168.6.0/24',
    [string]$WorkerPort = '8000',
    [string]$AcrRg = 'rg-confidential-mcp',
    [string]$AcrName = 'acrconfmcpvisdzn',
    [string]$DnsResolverIp = '192.168.1.36',
    [string]$WorkerZone = 'workers.pool.internal',
    [string]$NatGatewayId = "/subscriptions/7ee2b43a-eaea-4259-be7b-c8c220bfbcf9/resourceGroups/rg-confidential-mcp/providers/Microsoft.Network/natGateways/natgw-aci",
    [bool]$AttachNat = $true,
    [bool]$AllowImagePullEgress = $true,
    [string]$LogWorkspaceName = 'law-tracing-fsnn',
    [string]$LogWorkspaceRg = 'rg-fabric-foundry-eus2'
)
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$out = Join-Path $here 'out'
New-Item -ItemType Directory -Force $out | Out-Null
az account set --subscription $Subscription

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
        $nsg = Deploy-Bicep $ControlRg 'pool-nsg' 'nsg.bicep' @("controlCidr=$ControlCidr", "workerCidr=$WorkerCidr", "workerPort=$WorkerPort", "dnsResolverIp=$DnsResolverIp", "allowImagePullEgress=$($AllowImagePullEgress.ToString().ToLower())")
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
        $null = Deploy-Bicep $ControlRg 'pool-identities' 'identities.bicep' @()
    }
    'state' {
        Ensure-Rg $ControlRg
        $null = Deploy-Bicep $ControlRg 'pool-state' 'state.bicep' @("storageName=$storageName", "tableName=$tableName", "vnetId=$vnetId", "peSubnetId=$peSubnetId")
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
        $r = Deploy-Bicep $ControlRg 'pool-environment' 'environment.bicep' @("infraSubnetId=$controlSubnetId", "logWorkspaceName=$LogWorkspaceName", "logWorkspaceRg=$LogWorkspaceRg")
        if ($r) { $r.properties.outputs | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 (Join-Path $out 'environment.json') }
    }
}
