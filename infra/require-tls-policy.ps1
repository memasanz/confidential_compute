param(
    [string]$TemplatePath = (Join-Path $PSScriptRoot "template-vnet.json")
)

$ErrorActionPreference = "Stop"
$template = Get-Content -LiteralPath $TemplatePath -Raw | ConvertFrom-Json
$groups = @($template.resources | Where-Object { $_.type -eq "Microsoft.ContainerInstance/containerGroups" })
if ($groups.Count -ne 1) {
    throw "Expected exactly one container group in the template."
}

$policySettings = $groups[0].properties.confidentialComputeProperties
$policy = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($policySettings.ccePolicy))
$match = [regex]::Match($policy, '(?m)^containers\s*:=\s*(\[.*\])\r?$')
if (-not $match.Success) {
    throw "Cannot find generated container definitions in the CCE policy."
}
$containers = @($match.Groups[1].Value | ConvertFrom-Json)
$servers = @($containers | Where-Object { $_.name -eq "mcp-server" })
if ($servers.Count -ne 1) {
    throw "Expected exactly one mcp-server policy definition."
}

foreach ($name in @("MCP_TLS_CERT", "MCP_TLS_KEY")) {
    $patterns = @("$name=.*", "(?s:$name=.+)")
    $rules = @($servers[0].env_rules | Where-Object { $_.pattern -in $patterns })
    if ($rules.Count -ne 1 -or $rules[0].strategy -ne "re2") {
        throw "Expected one generated wildcard rule for $name. Generate the policy with --approve-wildcards first."
    }
    # PEM values span lines; required rules prevent silent plaintext fallback.
    $rules[0].pattern = "(?s:$name=.+)"
    $rules[0].required = $true
}

$definitions = ConvertTo-Json -InputObject $containers -Depth 100 -Compress
$policy = $policy.Remove($match.Groups[1].Index, $match.Groups[1].Length).Insert($match.Groups[1].Index, $definitions)
$bytes = [Text.Encoding]::UTF8.GetBytes($policy)
$policySettings.ccePolicy = [Convert]::ToBase64String($bytes)
$template | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $TemplatePath -Encoding utf8
$sha = [Security.Cryptography.SHA256]::Create()
try {
    [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace("-", "").ToLowerInvariant()
} finally {
    $sha.Dispose()
}
