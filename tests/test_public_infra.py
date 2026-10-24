"""Offline deployment contract tests; every Azure boundary is a local PowerShell fake."""

import base64
import json
from pathlib import Path
import shutil
import subprocess
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[1]
POOL = ROOT / "infra" / "pool"
PWSH = shutil.which("pwsh")
SUBSCRIPTION = "00000000-0000-4000-8000-000000000001"
TENANT = "00000000-0000-4000-8000-000000000002"
DIGEST = "sha256:" + "a" * 64
SVN = {name: 1 for name in ("bootloader", "tee", "snpfw", "microcode", "guest", "uvm_guest")}

pytestmark = pytest.mark.skipif(not PWSH, reason="PowerShell is not installed")


def run_script(script, parameters):
    encoded = base64.b64encode(json.dumps(parameters).encode()).decode()
    command = r"""
$global:calls = [System.Collections.Generic.List[object]]::new()
function global:az {
    $global:LASTEXITCODE = 0
    $global:calls.Add(@($args))
    $command = $args -join ' '
    if ($command -like 'group exists *') { return 'true' }
    if ($command -like 'identity show *') {
        return '{"id":"/subscriptions/test/resourceGroups/control/providers/Microsoft.ManagedIdentity/userAssignedIdentities/pull","c":"test-client"}'
    }
    if ($command -like 'containerapp env show *') {
        if ($command -like '*properties.defaultDomain -o tsv') { return 'pool.example.invalid' }
        return '{"d":"pool.example.invalid","ip":"10.20.1.4"}'
    }
    if ($command -like 'deployment group create *') { return '{"properties":{"outputs":{}}}' }
}
$p = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PARAMETERS__')) | ConvertFrom-Json -AsHashtable
$errorText = $null
try { $null = & '__SCRIPT__' @p } catch { $errorText = $_.Exception.Message }
'__RESULT__' + (ConvertTo-Json -Depth 20 -Compress -InputObject @{error=$errorText; calls=@($global:calls)})
"""
    command = command.replace("__PARAMETERS__", encoded).replace(
        "__SCRIPT__", str(POOL / script).replace("'", "''")
    )
    completed = subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand",
         base64.b64encode(command.encode("utf-16-le")).decode()],
        cwd=ROOT, capture_output=True, text=True, timeout=90, check=True,
    )
    result = next(line for line in completed.stdout.splitlines() if line.startswith("__RESULT__"))
    return json.loads(result[len("__RESULT__"):])


@pytest.fixture
def operational_directory():
    # Never use the system temp directory: generated secret-shaped files stay inside the repo.
    relative = Path("tests") / (".public-infra-" + uuid.uuid4().hex)
    directory = ROOT / relative
    (directory / "pki").mkdir(parents=True)
    for name in ("client-ca.pem", "gateway-client.pem", "gateway-client.key"):
        (directory / "pki" / name).write_text("offline-test-not-a-key", encoding="utf-8")
    try:
        yield str(relative)
    finally:
        shutil.rmtree(directory)


def app_parameters(directory):
    return {
        "Stage": "apps", "Subscription": SUBSCRIPTION, "Location": "westus3",
        "VnetRg": "network-rg", "VnetName": "test-vnet", "AcrName": "testregistry",
        "TenantId": TENANT, "ApiAudience": "api://test-pool", "ImageDigest": DIGEST,
        "WorkerImageDigest": "sha256:" + "b" * 64,
        "SidecarImageDigest": "sha256:" + "c" * 64,
        "WorkerSubnetName": "custom-workers", "WorkerCidr": "10.20.4.0/24",
        "WorkerZone": "workers.example.invalid", "MaaIssuer": "https://maa.example.invalid/",
        "MinimumSvn": json.dumps(SVN), "AllowPullRuntimeIdentity": True,
        "PullRoleAttested": "acr-pull-only", "MaterialAclConfirmed": "controller-only",
        "EnvName": "custom-environment", "PoolId": "custom-pool",
        "OperationalDirectory": directory, "WhatIfOnly": True,
    }


@pytest.mark.parametrize("script,stages", [
    ("deploy-pool-infra.ps1", ["preflight", "network", "identity", "state", "dns", "rbac", "environment"]),
    ("deploy-pool-apps.ps1", ["ingress-dns", "build", "apps"]),
])
def test_every_stage_requires_subscription_before_azure(script, stages):
    for stage in stages:
        result = run_script(script, {"Stage": stage})
        assert "-Subscription" in result["error"]
        assert result["calls"] == []


@pytest.mark.parametrize("stage,extra,missing", [
    ("network", {"Location": "westus3", "VnetRg": "network", "VnetName": "vnet"}, "ControlCidr"),
    ("network", {"Location": "westus3", "VnetRg": "network", "VnetName": "vnet",
                 "ControlCidr": "10.20.1.0/27", "WorkerCidr": "10.20.4.0/24",
                 "VnetCidr": "10.20.0.0/16"}, "DnsResolverIp"),
    ("state", {"Location": "westus3", "VnetRg": "network", "VnetName": "vnet"}, "PeSubnetName"),
    ("rbac", {"VnetRg": "network", "VnetName": "vnet"}, "AcrRg"),
    ("environment", {"Location": "westus3", "VnetRg": "network", "VnetName": "vnet"}, "LogWorkspaceName"),
])
def test_infra_stage_inputs_fail_before_azure(stage, extra, missing):
    result = run_script("deploy-pool-infra.ps1", {"Stage": stage, "Subscription": SUBSCRIPTION, **extra})
    assert f"-{missing}" in result["error"]
    assert result["calls"] == []


@pytest.mark.parametrize("field,value", [
    ("ImageDigest", "latest"), ("WorkerImageDigest", "sha256:abc"),
    ("SidecarImageDigest", ""), ("TenantId", "not-a-tenant"),
    ("WorkerCidr", "10.20.4.1/24"), ("WorkerCidr", "300.1.2.0/24"),
    ("MaaIssuer", "http://maa.example.invalid"), ("MaaIssuer", "https://maa.example.invalid/path"),
    ("MinimumSvn", "{}"), ("MinimumSvn", "null"),
    ("MinimumSvn", json.dumps({**SVN, "guest": True})),
    ("MinimumSvn", json.dumps({**SVN, "guest": -1})),
    ("MinimumSvn", json.dumps({**SVN, "guest": 1.5})),
    ("MinimumSvn", json.dumps({**SVN, "extra": 1})),
    ("AllowPullRuntimeIdentity", False), ("PullRoleAttested", "unreviewed"),
    ("MaterialAclConfirmed", "unreviewed"), ("DesiredReady", 4),
])
def test_apps_reject_invalid_or_unapproved_values_before_azure(operational_directory, field, value):
    parameters = app_parameters(operational_directory)
    parameters[field] = value
    result = run_script("deploy-pool-apps.ps1", parameters)
    assert result["error"], (field, result)
    assert result["calls"] == []


@pytest.mark.parametrize("field", [
    "Location", "AcrName", "VnetRg", "VnetName", "TenantId", "ApiAudience",
    "ImageDigest", "WorkerImageDigest", "SidecarImageDigest", "WorkerCidr",
    "MaaIssuer", "MinimumSvn", "PullRoleAttested", "MaterialAclConfirmed",
])
def test_apps_require_each_environment_input(operational_directory, field):
    parameters = app_parameters(operational_directory)
    del parameters[field]
    result = run_script("deploy-pool-apps.ps1", parameters)
    assert f"-{field}" in result["error"]
    assert result["calls"] == []


@pytest.mark.parametrize("allow_smt", [None, True])
def test_apps_generated_contract_and_what_if(operational_directory, allow_smt):
    parameters = app_parameters(operational_directory)
    if allow_smt is not None:
        parameters["AllowSmt"] = allow_smt
    result = run_script("deploy-pool-apps.ps1", parameters)
    assert result["error"] is None, result
    document = json.loads((ROOT / operational_directory / "apps.parameters.json").read_text(encoding="utf-8-sig"))
    p = {name: item["value"] for name, item in document["parameters"].items()}
    assert p["location"] == "westus3"
    assert p["environmentName"] == "custom-environment"
    assert p["image"] == f"testregistry.azurecr.io/pool-control@{DIGEST}"
    common, controller = p["common"], p["controllerEnv"]
    assert common["POOL_SUBNET_CIDR"] == "10.20.4.0/24"
    assert common["POOL_DNS_ZONE"] == "workers.example.invalid"
    assert common["POOL_MAA_ISSUER"] == "https://maa.example.invalid"
    assert json.loads(common["POOL_MINIMUM_SVN"]) == SVN
    assert common["POOL_ALLOW_SMT"] == str(bool(allow_smt)).lower()
    assert common["POOL_DESIRED_READY"] == 0
    assert controller["POOL_SUBNET_ID"].endswith("/test-vnet/subnets/custom-workers")
    assert controller["POOL_LOCATION"] == "westus3"
    assert controller["POOL_POOL_ID"] == "custom-pool"
    assert controller["POOL_ALLOW_PULL_RUNTIME_IDENTITY"] == "true"
    assert controller["POOL_PULL_ROLE_ATTESTED"] == "acr-pull-only"
    assert controller["POOL_MATERIAL_ACL_CONFIRMED"] == "controller-only"
    assert controller["POOL_WORKER_IMAGE"].endswith("@" + parameters["WorkerImageDigest"])
    assert controller["POOL_SIDECAR_IMAGE"].endswith("@" + parameters["SidecarImageDigest"])
    commands = [" ".join(call) for call in result["calls"]]
    assert any("deployment group what-if" in call for call in commands)
    assert any("deployment group validate" in call for call in commands)
    assert not any("deployment group create" in call for call in commands)


def test_apps_deploy_still_previews_and_validates_first(operational_directory):
    parameters = app_parameters(operational_directory)
    parameters["WhatIfOnly"] = False
    result = run_script("deploy-pool-apps.ps1", parameters)
    assert result["error"] is None, result
    deployments = [call[2] for call in result["calls"] if call[:2] == ["deployment", "group"]]
    assert deployments == ["what-if", "validate", "create"]


def test_apps_missing_local_material_fails_before_azure(operational_directory):
    (ROOT / operational_directory / "pki" / "gateway-client.key").unlink()
    result = run_script("deploy-pool-apps.ps1", app_parameters(operational_directory))
    assert "Missing operational input" in result["error"]
    assert result["calls"] == []


def test_removed_material_stage_is_rejected_before_azure():
    result = run_script("deploy-pool-apps.ps1", {
        "Stage": "material", "Subscription": SUBSCRIPTION, "Location": "westus3",
        "VnetRg": "network", "VnetName": "vnet", "PeSubnetName": "custom-pe",
        "EnvName": "custom-environment", "WhatIfOnly": True,
    })
    assert result["error"], result
    assert result["calls"] == []


def test_network_previews_explicit_cidrs_region_and_safe_egress():
    result = run_script("deploy-pool-infra.ps1", {
        "Stage": "network", "Subscription": SUBSCRIPTION, "Location": "westus3",
        "VnetRg": "network", "VnetName": "vnet", "ControlCidr": "10.20.1.0/27",
        "WorkerCidr": "10.20.4.0/24", "VnetCidr": "10.20.0.0/16",
        "DnsResolverIp": "10.20.2.4", "WorkerSubnetName": "custom-workers", "WhatIfOnly": True,
    })
    assert result["error"] is None, result
    preview = next(call for call in result["calls"] if call[:3] == ["deployment", "group", "what-if"])
    for expected in ("location=westus3", "controlCidr=10.20.1.0/27", "workerCidr=10.20.4.0/24",
                     "vnetCidr=10.20.0.0/16", "dnsResolverIp=10.20.2.4", "allowImagePullEgress=false"):
        assert expected in preview
    assert not any(call[:3] == ["deployment", "group", "create"] for call in result["calls"])


def test_build_cannot_silently_ignore_what_if():
    result = run_script("deploy-pool-apps.ps1", {
        "Stage": "build", "Subscription": SUBSCRIPTION, "AcrName": "testregistry", "WhatIfOnly": True,
    })
    assert "no what-if mode" in result["error"]
    assert result["calls"] == []


def test_nat_opt_in_requires_explicit_resource_id():
    result = run_script("deploy-pool-infra.ps1", {
        "Stage": "network", "Subscription": SUBSCRIPTION, "Location": "westus3",
        "VnetRg": "network", "VnetName": "vnet", "ControlCidr": "10.20.1.0/27",
        "WorkerCidr": "10.20.4.0/24", "VnetCidr": "10.20.0.0/16",
        "DnsResolverIp": "10.20.2.4", "AttachNat": True, "WhatIfOnly": True,
    })
    assert "-NatGatewayId" in result["error"]
    assert result["calls"] == []


def test_bicep_has_no_network_defaults_and_retains_policy_boundaries():
    nsg = (POOL / "bicep" / "nsg.bicep").read_text()
    for name in ("controlCidr", "workerCidr", "vnetCidr", "dnsResolverIp"):
        assert f"param {name} string\n" in nsg
    assert "AzureContainerRegistry.${location}" in nsg
    assert "Storage.${location}" in nsg
    for name in ("dns", "ingress-dns", "state"):
        assert "name: 'link-pool-vnet'" in (POOL / "bicep" / f"{name}.bicep").read_text()
    apps = (POOL / "bicep" / "apps.bicep").read_text()
    for secret in ("clientCaCert", "gatewayClientKey"):
        assert f"@secure()\nparam {secret} string" in apps
    assert "serverCa" not in apps and "POOL_CA_KEY" not in apps
    script = (POOL / "deploy-pool-apps.ps1").read_text()
    assert "POOL_POLICY_BASE = '/app/infra/pool/policy-base.json'" in script
