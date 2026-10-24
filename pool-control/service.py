"""Trusted service entrypoints, not an Azure deployment command.

Required common environment:
POOL_IDLE, POOL_ABSOLUTE, POOL_QUEUE_WAIT, POOL_QUEUE_SIZE, POOL_OPERATION,
POOL_CLAIM, POOL_UPLOAD_BYTES, POOL_RESULT_BYTES, POOL_PROVISIONING_AGE,
POOL_TERMINAL_RETENTION, POOL_SDK_TIMEOUT, POOL_TABLE_TIMEOUT: explicit positive integers;
POOL_OPERATION < POOL_CLAIM, POOL_QUEUE_SIZE <=50.
POOL_TABLE_ENDPOINT, POOL_TABLE_NAME (precreated), POOL_PARTITION,
POOL_IDENTITY_CLIENT_ID (separate gateway/controller user-assigned identities);
POOL_CLIENT_CERT, POOL_CLIENT_KEY (existing file paths),
POOL_DNS_ZONE, POOL_SUBNET_CIDR, POOL_MAA_ISSUER, POOL_MINIMUM_SVN (JSON).
Optional POOL_ALLOW_SMT must be exactly true or false (default fail-closed false).

Gateway additionally: POOL_ENTRA_TENANT, POOL_ENTRA_AUDIENCE,
POOL_ALLOWED_ROLES and/or POOL_ALLOWED_SCOPES (comma-separated),
POOL_GATEWAY_URL, POOL_BIND_HOST, POOL_BIND_PORT.
Controller additionally: POOL_SUBSCRIPTION, POOL_RESOURCE_GROUP, POOL_POOL_ID,
POOL_RESOURCE_PREFIX, POOL_LOCATION, POOL_SUBNET_ID, POOL_DNS_RESOURCE_GROUP,
POOL_RECONCILE_SECONDS, POOL_MATERIAL_FACTORY=importable_module:function.
Use POOL_MATERIAL_FACTORY=material:environment for the durable provider; see
material.py for required PKI/image/volume/CLI settings. Private ACR uses only
an explicitly approved attached user-assigned pull identity, never passwords:
POOL_PULL_IDENTITY, POOL_REGISTRY_SERVER, POOL_ALLOW_PULL_RUNTIME_IDENTITY=true,
POOL_PULL_ROLE_ATTESTED=acr-pull-only. This REVERSES the prior no-runtime-identity
invariant: hostile worker code can request tokens for this identity; ensure its
ONLY effective privilege is AcrPull on the single approved registry. This code
does not create or verify RBAC assignments. No public/anonymous fallback.
SDK wall-clock timeout <=operation; claim >operation+2*table timeout.
Private hosting/RBAC/network policy and live Table/ARM/SKR behavior are rollout gates.

Run from repository root:
 .venv\\Scripts\\python.exe pool-control\\service.py gateway
 .venv\\Scripts\\python.exe pool-control\\service.py controller
 .venv\\Scripts\\python.exe pool-control\\service.py admin --action inspect
 .venv\\Scripts\\python.exe pool-control\\service.py admin --action retry-provisioning --worker ID --lifetime ID
 .venv\\Scripts\\python.exe pool-control\\service.py admin --action destroy --worker ID --lifetime ID --acknowledge-uncertain-create
 .venv\\Scripts\\python.exe pool-control\\service.py admin --action resume-admission
Admin uses the controller/storage identity, has no MCP tool and no forced-slot-free
action. Provisioning-age quarantine retains capacity. Destroy requires operator
confirmation that no create is still running/accepted-but-unobserved; retry is safer.
Over-cap owned orphans are recorded/drained, admission is paused, and deletion
must be confirmed before explicit resume-admission. Terminal GC removes records
only after the assigned worker record is gone, in batches of at most 20.
Gateway ingress TLS/private hosting must be supplied by approved hosting; bind
only on the configured private interface. This module changes no networking.
"""

import argparse
import asyncio
import importlib
import json
import logging
import os

from azure.identity import ManagedIdentityCredential

from auth import EntraVerifier
from config import Limits
from gateway import Gateway, build_mcp
from store import PoolStore, TableBackend
from transport import WorkerTransport
from bounded import BoundedAzure, BoundedTable


def table_factory():
    e = os.environ
    credential = ManagedIdentityCredential(client_id=e["POOL_IDENTITY_CLIENT_ID"])
    return TableBackend(e["POOL_TABLE_ENDPOINT"], e["POOL_TABLE_NAME"],
                        e["POOL_PARTITION"], credential)


def azure_factory():
    from azure_adapter import AzurePool
    from material import pull_configuration
    e = os.environ
    credential = ManagedIdentityCredential(client_id=e["POOL_IDENTITY_CLIENT_ID"])
    module, function = e["POOL_MATERIAL_FACTORY"].split(":")
    material = getattr(importlib.import_module(module), function)()
    pull = pull_configuration(e.get("POOL_PULL_IDENTITY"), e.get("POOL_REGISTRY_SERVER"),
                              e.get("POOL_ALLOW_PULL_RUNTIME_IDENTITY"), e.get("POOL_PULL_ROLE_ATTESTED"))
    return AzurePool(
        credential, e["POOL_SUBSCRIPTION"], e["POOL_RESOURCE_GROUP"],
        e["POOL_POOL_ID"], e["POOL_RESOURCE_PREFIX"], e["POOL_LOCATION"],
        e["POOL_SUBNET_ID"], e["POOL_SUBNET_CIDR"],
        e["POOL_DNS_RESOURCE_GROUP"], e["POOL_DNS_ZONE"], material, pull)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("gateway", "controller", "admin"))
    parser.add_argument("--action", choices=("inspect", "retry-provisioning", "destroy", "resume-admission"))
    parser.add_argument("--worker")
    parser.add_argument("--lifetime")
    parser.add_argument("--acknowledge-uncertain-create", action="store_true")
    args = parser.parse_args()
    env = os.environ
    limits = Limits.environment()
    sdk_timeout, table_timeout = int(env["POOL_SDK_TIMEOUT"]), int(env["POOL_TABLE_TIMEOUT"])
    if not (0 < sdk_timeout <= limits.operation and 0 < table_timeout
            and limits.claim > limits.operation + 2 * table_timeout):
        raise ValueError("SDK/table deadlines must fit within operation/claim fencing budget")
    store = PoolStore(BoundedTable(table_factory, table_timeout), limits)
    if args.mode == "admin":
        if args.action == "inspect":
            rows = store.view()
            print(json.dumps({k: {name: v[name] for name in
                                  ("id", "lifetime", "state", "created", "claim_until", "quarantine_reason",
                                   "admission_paused", "reason") if name in v}
                              for k, v in rows.items() if k.startswith("w:") or k == "control"}))
        else:
            if not args.action:
                parser.error("Admin requires an explicit action")
            if args.action == "destroy" and not args.acknowledge_uncertain_create:
                parser.error("Destroy requires --acknowledge-uncertain-create")
            print(json.dumps(store.recover(args.worker, args.action, args.lifetime)))
        return
    smt = env.get("POOL_ALLOW_SMT", "false")
    if smt not in ("true", "false"):
        raise ValueError("POOL_ALLOW_SMT must be true/false")
    transport = WorkerTransport(
        env["POOL_CLIENT_CERT"], env["POOL_CLIENT_KEY"],
        env["POOL_DNS_ZONE"], env["POOL_SUBNET_CIDR"], env["POOL_MAA_ISSUER"],
        json.loads(env["POOL_MINIMUM_SVN"]), limits, allow_smt=smt == "true")
    if args.mode == "gateway":
        verifier = EntraVerifier(
            env["POOL_ENTRA_TENANT"], env["POOL_ENTRA_AUDIENCE"],
            filter(None, env.get("POOL_ALLOWED_ROLES", "").split(",")),
            filter(None, env.get("POOL_ALLOWED_SCOPES", "").split(",")))
        build_mcp(Gateway(store, transport), verifier, env["POOL_GATEWAY_URL"],
                  env["POOL_BIND_HOST"], int(env["POOL_BIND_PORT"])).run(
                      transport="streamable-http")
    else:
        from controller import Controller
        logging.basicConfig(level=logging.WARNING)
        logging.getLogger("controller").setLevel(logging.INFO)
        # Validate all material inputs synchronously at startup before entering recovery loop.
        azure_factory()
        azure = BoundedAzure(azure_factory, sdk_timeout)
        asyncio.run(Controller(store, azure, transport).run(
            int(env["POOL_RECONCILE_SECONDS"])))


if __name__ == "__main__":
    main()
