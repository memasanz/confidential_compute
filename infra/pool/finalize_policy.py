"""Finalize a LOCAL generated pool policy; prints ONLY its FINAL trusted SHA256.

Pass a generated worker template and its local parameters after running:
 az confcom acipolicygen --template-file ... --parameters ... --approve-wildcards
Do not use an intermediate acipolicygen hash as expected CCE hostdata.
This script does not deploy, invoke Azure, or change non-worker allowances.
"""

import argparse
import base64
import hashlib
import json
import re
from pathlib import Path


def finalize(template, parameters):
    groups = [r for r in template["resources"] if r["type"] == "Microsoft.ContainerInstance/containerGroups"]
    if len(groups) != 1 or "identity" in groups[0]:
        raise ValueError("Expected exactly one identity-free worker group")
    group = groups[0]["properties"]
    if "imageRegistryCredentials" in group:
        raise ValueError("This spike template must not embed registry/fleet credentials")
    if group["sku"] != "Confidential":
        raise ValueError("Expected Confidential SKU")
    settings = group["confidentialComputeProperties"]
    policy = base64.b64decode(settings["ccePolicy"], validate=True).decode()
    match = re.search(r"(?m)^containers\s*:=\s*(\[.*\])\r?$", policy)
    if not match:
        raise ValueError("Missing generated container definitions")
    containers = json.loads(match[1])
    servers = [c for c in containers if c["name"] == "mcp-server"]
    if len(servers) != 1:
        raise ValueError("Expected one mcp-server policy")
    values = {k: v["value"] for k, v in parameters["parameters"].items()}
    expected = {
        "MCP_WORKER_MODE": "pooled", "MCP_WORKER_ID": values["workerId"],
        "MCP_SESSION_ID": values["sessionId"], "MCP_WORKER_HOSTNAME": values["workerHostname"],
        "MCP_TLS_CLIENT_CA": values["clientCa"], "SKR_MAA_ENDPOINT": values["maaEndpoint"],
        "ATTESTATION_SIDECAR_URL": "http://localhost:8080",
    }
    for name in ("MCP_WORKER_ID", "MCP_SESSION_ID"):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", expected[name]):
            raise ValueError("Explicit worker/session IDs must be safe identifiers")
    if "PRIVATE KEY" in expected["MCP_TLS_CLIENT_CA"]:
        raise ValueError("Worker client trust must not contain gateway or CA private keys")
    worker = next(c for c in group["containers"] if c["name"] == "mcp-server")
    env = {e["name"]: e.get("value", e.get("secureValue")) for e in worker["properties"]["environmentVariables"]}
    if set(env) != set(expected):
        raise ValueError("Unexpected worker configuration/authentication inputs")
    for name, raw in env.items():
        parameter = re.fullmatch(r"\[parameters\('([^']+)'\)\]", raw)
        resolved = values[parameter[1]] if parameter else raw
        if name in expected and resolved != expected[name]:
            raise ValueError(f"Unexpected configuration: {name}")
    rules = servers[0]["env_rules"]
    # A catch-all or alternate broad regex could bypass the pinned required rule.
    # Standard confcom/ACI platform rules, anchored to FABRIC_* and TEMP/TMP names.
    platform = {"(?i)(FABRIC)_.+=.+", "T(E)?MP=.+"}
    for rule in rules:
        if rule["pattern"] in platform:
            continue
        if not re.match(r"^(?:\(\?s:)?[A-Za-z_][A-Za-z0-9_]*=", rule["pattern"]):
            raise ValueError("Unsupported broad environment rule; inspect generated policy")
    forbidden = ("MCP_AUTH_", "ENTRA_", "SKR_AKV_", "MCP_TLS_CERTFILE", "MCP_TLS_KEYFILE",
                 "MCP_TLS_CLIENT_CAFILE")
    if any(any(prefix in rule["pattern"] for prefix in forbidden) for rule in rules):
        raise ValueError("Legacy authentication/path overrides are forbidden")
    for name in expected:
        matches = [r for r in rules if r["pattern"].startswith(name + "=")
                   or r["pattern"].startswith("(?s:" + name + "=")]
        if len(matches) != 1:
            raise ValueError(f"Expected one generated rule for {name}")
        rule = matches[0]
        # Literal public config and client CA are pinned; do not wildcard identities/trust.
        rule["strategy"] = "string"
        rule["pattern"] = f"{name}={expected[name]}"
        rule["required"] = True
    definitions = json.dumps(containers, separators=(",", ":"))
    policy = policy[:match.start(1)] + definitions + policy[match.end(1):]
    policy_bytes = policy.encode()
    settings["ccePolicy"] = base64.b64encode(policy_bytes).decode()
    return hashlib.sha256(policy_bytes).hexdigest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--parameters", type=Path, required=True)
    args = parser.parse_args()
    operational = (Path(".azure") / "pool-feasibility").resolve()
    if operational not in args.template.resolve().parents or operational not in args.parameters.resolve().parents:
        parser.error("Only ignored .azure\\pool-feasibility operational files may be finalized")
    template = json.loads(args.template.read_text(encoding="utf-8-sig"))
    result = finalize(template, json.loads(args.parameters.read_text(encoding="utf-8-sig")))
    args.template.write_text(json.dumps(template, indent=2), encoding="utf-8")
    print(result)
