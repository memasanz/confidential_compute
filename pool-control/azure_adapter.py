"""Live ARM/DNS adapter; no deployment occurs merely by importing this module.

Material provider MUST implement prepare(worker, hostname) -> {
  "group": final ARM container-group resource object,
  "policy_hash": finalized CCE SHA256
}, and forget(worker). prepare must be durable/idempotent by worker.id, include only public
gateway-client CA trust, preserve the fixed lifetime IDs, and perform approved private-image
pull integration. Workers generate their own TLS key, so no server certificate is issued.
CCE generation is intentionally not guessed here.

Private pull decision: documented ACI user-assigned managed identity authentication:
https://learn.microsoft.com/en-us/azure/container-instances/using-azure-container-registry-mi
The identity must be ATTACHED to the group, hence is runtime-token-accessible.
There is no asserted "pull-only invisible identity" feature. ACR anonymous/public
mirrors give up authenticated private distribution; registry passwords introduce
secrets. Neither is an automatic fallback. The approved opt-in identity must have
only AcrPull on the one registry, no storage/management/HSM/data permissions.
This deliberately reverses the earlier identity-free runtime requirement.
Network-restricted ACR requires trusted-services/private endpoint configuration
as documented, outside this code's authority. ABAC-enabled registries require
their documented repository-read role rather than AcrPull: not supported by the
current explicit AcrPull assertion and must not silently broaden assignments.
"""

import base64
import hashlib
import ipaddress
import re
import time
from datetime import datetime, timezone

from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.containerinstance import ContainerInstanceManagementClient
from azure.mgmt.containerinstance.models import ContainerGroup
from azure.mgmt.privatedns import PrivateDnsManagementClient
from errors import WorkerFailed


class AzurePool:
    def __init__(self, credential, subscription, resource_group, pool, prefix,
                 location, subnet_id, subnet_cidr, dns_group, dns_zone, material, pull=None):
        if not re.fullmatch(r"[a-z][a-z0-9-]{2,30}", prefix):
            raise ValueError("Explicit dedicated resource prefix required")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", pool):
            raise ValueError("Safe pool identifier required")
        if f"/subscriptions/{subscription}/resourceGroups/" not in subnet_id:
            raise ValueError("Explicit same-subscription pool subnet required")
        self.rg, self.pool, self.prefix, self.location = resource_group, pool, prefix, location
        self.subnet_id, self.network = subnet_id, ipaddress.ip_network(subnet_cidr)
        if not self.network.is_private:
            raise ValueError("Private worker subnet required")
        self.dns_group, self.zone, self.material = dns_group, dns_zone, material
        self.pull = pull
        # ACI create/delete accept responses can exceed the 20 s read bound; the spawned-SDK deadline still caps them.
        self.write_timeout = 120
        self.groups = ContainerInstanceManagementClient(
            credential, subscription, retry_total=0, connection_timeout=10,
            read_timeout=20, polling_interval=5).container_groups
        self.dns = PrivateDnsManagementClient(
            credential, subscription, retry_total=0,
            connection_timeout=10, read_timeout=20).record_sets

    def name(self, worker):
        if not re.fullmatch(r"[0-9a-f]{32}", worker["id"]):
            raise ValueError("Invalid durable worker ID")
        return self.prefix + "-" + worker["id"]

    def tags(self, worker):
        return {"pool-control": self.pool, "pool-worker": worker["id"],
                "pool-lifetime": worker["lifetime"], "pool-version": "1"}

    def owned(self, group, worker):
        if group.name != self.name(worker) or group.tags != self.tags(worker):
            raise PermissionError("Refusing unrelated or legacy container group")
        return group

    def get(self, worker):
        try:
            return self.owned(self.groups.get(self.rg, self.name(worker)), worker)
        except ResourceNotFoundError:
            return None

    def inventory(self):
        owned = {}
        for g in self.groups.list_by_resource_group(self.rg):
            tags = g.tags or {}
            wid, lifetime = tags.get("pool-worker", ""), tags.get("pool-lifetime", "")
            if (tags.get("pool-control") == self.pool and tags.get("pool-version") == "1"
                    and re.fullmatch(r"[0-9a-f]{32}", wid)
                    and re.fullmatch(r"[0-9a-f]{32}", lifetime)):
                worker = {"id": wid, "lifetime": lifetime}
                self.owned(g, worker)
                owned[wid] = lifetime
        return owned

    def healthy(self, worker):
        group = self.get(worker)
        return (group is not None and group.provisioning_state == "Succeeded"
                and self.running(group))

    @staticmethod
    def running(group):
        return all(c.instance_view and c.instance_view.current_state
                   and c.instance_view.current_state.state == "Running"
                   for c in group.containers)

    def validate_material(self, worker, hostname, material):
        group = material["group"]
        p = group["properties"]
        policy = base64.b64decode(p["confidentialComputeProperties"]["ccePolicy"], validate=True)
        if not policy or hashlib.sha256(policy).hexdigest() != material["policy_hash"]:
            raise ValueError("Finalized CCE policy/hash required")
        if (group["location"].lower() != self.location.lower() or p["sku"] != "Confidential"
                or p["ipAddress"]["type"] != "Private"
                or p["subnetIds"] != [{"id": self.subnet_id}]):
            raise ValueError("Material outside approved confidential/network/identity bounds")
        if (group.get("name", self.name(worker)) != self.name(worker)
                or p.get("osType") != "Linux" or p.get("initContainers")
                or p.get("extensions") or p.get("volumes") or p.get("diagnostics")
                or p.get("encryptionProperties")):
            raise ValueError("Unsupported worker material")
        pull = getattr(self, "pull", None)
        if pull:
            if (group.get("identity") != {"type": "UserAssigned",
                                         "userAssignedIdentities": {pull["identity"]: {}}}
                    or p.get("imageRegistryCredentials") != [
                        {"server": pull["server"], "identity": pull["identity"]}]):
                raise ValueError("Only the explicitly approved pull-only identity is permitted")
        elif group.get("identity") or p.get("imageRegistryCredentials"):
            raise ValueError("Runtime pull identity not explicitly approved")
        containers = p["containers"]
        if {c["name"] for c in containers} != {"mcp-server", "attestation-sidecar"} or len(containers) != 2:
            raise ValueError("One worker and one attestation sidecar required")
        for container in containers:
            if not re.fullmatch(r".+@sha256:[0-9a-f]{64}", container["properties"]["image"]):
                raise ValueError("Both approved images must be pinned by digest")
            if pull and not container["properties"]["image"].startswith(pull["server"] + "/"):
                raise ValueError("Image outside approved private registry")
            expected_resources = {"cpu": 2, "memoryInGB": 4} if container["name"] == "mcp-server" else {"cpu": 1, "memoryInGB": 1}
            if container["properties"]["resources"]["requests"] != expected_resources:
                raise ValueError("Only the reviewed 3-vCPU/5-GB group shape is supported")
            if (container["name"] == "attestation-sidecar"
                    and container["properties"].get("environmentVariables")):
                raise ValueError("No sidecar credentials/environment are supported")
        env = next(c for c in containers if c["name"] == "mcp-server")["properties"]["environmentVariables"]
        values = {v["name"]: v.get("value", v.get("secureValue")) for v in env}
        expected = {"MCP_WORKER_MODE": "pooled", "MCP_WORKER_ID": worker["id"],
                    "MCP_SESSION_ID": worker["lifetime"], "MCP_WORKER_HOSTNAME": hostname}
        if any(values.get(k) != v for k, v in expected.items()):
            raise ValueError("Material differs from reserved worker identity")
        # No MCP_TLS_CERT/MCP_TLS_KEY: the worker generates its TLS key inside the TEE.
        allowed = set(expected) | {"MCP_TLS_CLIENT_CA", "SKR_MAA_ENDPOINT",
                                  "ATTESTATION_SIDECAR_URL", "SANDBOX_WORKSPACE", "SANDBOX_COMMAND_TIMEOUT"}
        if set(values) - allowed or not values.get("MCP_TLS_CLIENT_CA"):
            raise ValueError("Unapproved worker environment or missing mTLS")
        if "PRIVATE KEY" in values["MCP_TLS_CLIENT_CA"]:
            raise ValueError("Never inject client CA private keys")
        from cryptography import x509
        now = datetime.now(timezone.utc)
        roots = x509.load_pem_x509_certificates(values["MCP_TLS_CLIENT_CA"].encode())
        if not roots or any(
                not c.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
                or not c.not_valid_before_utc <= now < c.not_valid_after_utc
                for c in roots):
            raise ValueError("Dedicated currently valid public client CA required")

    def ensure(self, worker):
        hostname = worker["id"] + "." + self.zone
        material = self.material.prepare(worker, hostname)
        self.validate_material(worker, hostname, material)
        group = self.get(worker)
        if group is None:
            if time.time() >= worker["claim_until"]:
                raise TimeoutError("Provisioning claim expired before create")
            body = dict(material["group"])
            body["tags"] = self.tags(worker)
            self.groups.begin_create_or_update(
                self.rg, self.name(worker), ContainerGroup.from_dict(body),
                headers={"x-ms-client-request-id": worker["create_operation"]}, read_timeout=self.write_timeout)
            return None
        if group.provisioning_state in ("Failed", "Canceled"):
            raise WorkerFailed("Worker provisioning failed")
        if group.provisioning_state != "Succeeded":
            return None
        if not self.running(group):
            return None
        address = ipaddress.ip_address(group.ip_address.ip)
        if address not in self.network:
            raise ValueError("Worker address outside approved subnet")
        self.put_dns(worker, str(address))
        return {"hostname": hostname, "ip": str(address),
                "policy_hash": material["policy_hash"]}

    def put_dns(self, worker, address):
        try:
            previous = self.dns.get(self.dns_group, self.zone, "A", worker["id"])
            if previous.metadata != self.tags(worker):
                raise PermissionError("Refusing unrelated DNS record")
            condition = {"if_match": previous.etag}
        except ResourceNotFoundError:
            condition = {"if_none_match": "*"}
        self.dns.create_or_update(self.dns_group, self.zone, "A", worker["id"],
                                  {"ttl": 30, "a_records": [{"ipv4_address": address}],
                                   "metadata": self.tags(worker)}, **condition)

    def destroy(self, worker):
        group = self.get(worker)
        if group is not None:
            self.groups.begin_delete(self.rg, self.name(worker),
                                     headers={"x-ms-client-request-id": worker["delete_operation"]}, read_timeout=self.write_timeout)
            return False
        # Only a confirmed ARM 404 frees capacity; also finish owned DNS/PKI cleanup.
        try:
            record = self.dns.get(self.dns_group, self.zone, "A", worker["id"])
            if record.metadata != self.tags(worker):
                raise PermissionError("Refusing unrelated DNS record")
            self.dns.delete(self.dns_group, self.zone, "A", worker["id"], if_match=record.etag)
        except ResourceNotFoundError:
            pass
        self.material.forget(worker)
        return True
