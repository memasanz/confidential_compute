"""Durable controller-only PKI + actual confcom policy generation.

Factory: POOL_MATERIAL_FACTORY=material:environment.
Required: POOL_MATERIAL_ROOT (controller-only persistent volume),
POOL_CA_CERT, POOL_CA_KEY, POOL_GATEWAY_CLIENT_CA (existing PEM paths),
POOL_WORKER_IMAGE, POOL_SIDECAR_IMAGE (approved digest references),
POOL_CERT_DAYS, POOL_CONFCOM_TIMEOUT, POOL_AZ_EXECUTABLE (e.g. az.cmd),
POOL_RESOURCE_PREFIX, POOL_LOCATION, POOL_SUBNET_ID, POOL_MAA_ISSUER.
Private pull requires POOL_PULL_IDENTITY, POOL_REGISTRY_SERVER and explicit
POOL_ALLOW_PULL_RUNTIME_IDENTITY=true, and POOL_PULL_ROLE_ATTESTED=acr-pull-only.
The latter is an operator assertion, NOT an RBAC verification.

Only an existing worker server CA is used: no CA creation or Azure mutation.
The CA private key stays on the trusted controller volume. Worker private keys
stay in per-worker controller material, never Table. Restrict the volume ACL to
the controller identity/operators; backups share that trust boundary.
confcom can invoke Docker/pull images locally; approval/network access required.
Identity is added to confcom input, then stripped ONLY from the copy passed to
the existing identity-free finalizer. No synthetic CCE policy is generated.
"""

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra" / "pool"))
from finalize_policy import finalize


@contextmanager
def file_lock(path):
    """OS advisory lock released on process death; lock files are never unlinked."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def atomic_json(path, value):
    replacement = path.with_suffix(".new")
    descriptor = os.open(replacement, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(replacement, path)


def pull_configuration(identity, registry, consent, role_assertion):
    if not identity and not registry:
        return None
    if consent != "true" or role_assertion != "acr-pull-only":
        raise ValueError("Private image pull requires explicit runtime identity/AcrPull approval")
    if (not re.fullmatch(r"/subscriptions/[0-9a-fA-F-]+/resourceGroups/[^/]+/providers/Microsoft.ManagedIdentity/userAssignedIdentities/[^/]+",
                         identity or "", re.IGNORECASE)
            or not re.fullmatch(r"[a-z0-9]+\.azurecr\.io", registry or "")):
        raise ValueError("Explicit user-assigned pull identity and ACR login server required")
    return {"identity": identity, "server": registry}


def worker_template(images, pull):
    template = json.loads((Path(__file__).resolve().parents[1] / "infra" / "pool" / "worker.example.json").read_text())
    group = template["resources"][0]
    group["properties"]["containers"][1]["properties"]["image"] = images[1]
    if pull:
        if not all(i.startswith(pull["server"] + "/") for i in images):
            raise ValueError("Both images must use the approved private pull registry")
        group["identity"] = {"type": "UserAssigned", "userAssignedIdentities": {pull["identity"]: {}}}
        group["properties"]["imageRegistryCredentials"] = [
            {"server": pull["server"], "identity": pull["identity"]}]
    return template


def template_shape(template):
    """Hash of everything except the policy: a base policy is valid only for this exact shape."""
    clone = copy.deepcopy(template)
    clone["resources"][0]["properties"]["confidentialComputeProperties"]["ccePolicy"] = ""
    return hashlib.sha256(json.dumps(clone, sort_keys=True).encode()).hexdigest()


def policy_injector(base_path, images, pull):
    """Runner that injects an operator-generated base policy instead of running confcom/Docker.

    The base (infra/pool/make_policy_base.py, run offline with confcom + Docker) is generated
    with every per-worker value left as a wildcard; the unchanged finalizer then pins them.
    It is bound to the exact images and template shape, so any drift fails closed.
    """
    base = json.loads(Path(base_path).read_text(encoding="utf-8"))
    if (base.get("images") != list(images)
            or base.get("shape") != template_shape(worker_template(images, pull))
            or not base.get("ccePolicy")):
        raise ValueError("Base policy does not match the approved images/template; regenerate it")

    def inject(template_path, _parameters):
        template = json.loads(Path(template_path).read_text(encoding="utf-8"))
        if template_shape(template) != base["shape"]:
            raise ValueError("Worker template differs from the base policy shape")
        template["resources"][0]["properties"]["confidentialComputeProperties"]["ccePolicy"] = base["ccePolicy"]
        atomic_json(Path(template_path), template)
    return inject


def derived_worker_key(ca_key, worker_id, lifetime):
    """Deterministic P-256 worker TLS key bound to the CA secret, worker ID and lifetime."""
    secret = ca_key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption())
    okm = HKDF(hashes.SHA256(), 48, (worker_id + ":" + lifetime).encode(),
               b"pool-worker-tls-v1").derive(secret)
    order = int("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16)
    return ec.derive_private_key(int.from_bytes(okm, "big") % (order - 1) + 1, ec.SECP256R1())


class MaterialProvider:
    def __init__(self, root, ca_cert, ca_key, client_ca, worker_image, sidecar_image,
                 prefix, location, subnet, maa_issuer, cert_days, command_timeout,
                 az_executable, pull=None, runner=None, policy_base=None):
        self.root = Path(root).resolve()
        if Path.cwd().resolve() not in self.root.parents:
            raise ValueError("Material volume must be beneath the service working directory")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.ca_cert = x509.load_pem_x509_certificate(Path(ca_cert).read_bytes())
        self.ca_key = serialization.load_pem_private_key(Path(ca_key).read_bytes(), password=None)
        self.client_ca = Path(client_ca).read_text()
        if (not self.ca_cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
                or self.ca_key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
                != self.ca_cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
                or "PRIVATE KEY" in self.client_ca):
            raise ValueError("Existing matching server CA and public client trust required")
        if not isinstance(self.ca_key, rsa.RSAPrivateKey):
            raise ValueError("RSA server CA required: deterministic PKCS#1 v1.5 signatures keep re-derived worker certificates identical")
        for image in (worker_image, sidecar_image):
            if not re.fullmatch(r".+@sha256:[0-9a-f]{64}", image):
                raise ValueError("Approved images must be digest pinned")
        if cert_days <= 0 or command_timeout <= 0:
            raise ValueError("Explicit certificate lifetime and confcom timeout required")
        if not maa_issuer.startswith("https://") or "/" in maa_issuer[8:].strip("/"):
            raise ValueError("Pinned MAA HTTPS origin required")
        self.images, self.prefix, self.location = (worker_image, sidecar_image), prefix, location
        self.subnet, self.maa = subnet, maa_issuer.rstrip("/")[8:]
        self.days, self.timeout, self.az, self.pull = cert_days, command_timeout, az_executable, pull
        if policy_base and runner:
            raise ValueError("Use either an injected runner or a base policy")
        self.runner = (policy_injector(policy_base, self.images, pull) if policy_base
                       else runner or self.generate)
        self.configuration = hashlib.sha256(json.dumps({
            "images": self.images, "prefix": prefix, "location": location, "subnet": subnet,
            "maa": self.maa, "pull": pull,
            "issuer": self.ca_cert.public_bytes(serialization.Encoding.PEM).decode(),
            "client_ca": self.client_ca}, sort_keys=True).encode()).hexdigest()

    def generate(self, template, parameters):
        # No stdout/stderr diagnostics: confcom failures can expose secure parameters.
        command = [self.az, "confcom", "acipolicygen", "--template-file", str(template),
                   "--parameters", str(parameters), "--approve-wildcards"]
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            process.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                # Specific process tree only, including Docker/confcom helper descendants.
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            else:
                process.kill()
            process.wait(timeout=15)
            raise TimeoutError("confcom deadline exceeded") from None
        if process.returncode:
            raise RuntimeError("confcom generation failed; no worker may be provisioned")

    def prepare(self, worker, hostname):
        wid = worker["id"]
        if not re.fullmatch(r"[0-9a-f]{32}", wid) or not re.fullmatch(r"[0-9a-f]{32}", worker["lifetime"]):
            raise ValueError("Reserved worker/lifetime identities required")
        if hostname.split(".")[0] != wid or not re.fullmatch(r"[a-z0-9.-]+", hostname):
            raise ValueError("Exact reserved DNS hostname required")
        with file_lock(self.root / "locks" / (wid + ".lock")):
            folder = self.root / wid
            if (self.root / "retired" / (wid + ".json")).exists():
                raise PermissionError("Material was retired; worker ID may never be reused")
            folder.mkdir(exist_ok=True, mode=0o700)
            binding = {"worker": wid, "lifetime": worker["lifetime"], "hostname": hostname,
                       "configuration": self.configuration}
            binding_path = folder / "binding.json"
            if binding_path.exists():
                if json.loads(binding_path.read_text()) != binding:
                    raise PermissionError("Persistent worker binding mismatch")
            else:
                atomic_json(binding_path, binding)
            material_path = folder / "material.json"
            if material_path.exists():
                material = json.loads(material_path.read_text())
                cert = x509.load_pem_x509_certificate(material["server_cert"].encode())
                if not cert.not_valid_before_utc <= datetime.now(timezone.utc) < cert.not_valid_after_utc:
                    raise ValueError("Persistent worker certificate expired; retire instead of rotating a used key")
                return material
            pair_path = folder / "tls.json"
            if pair_path.exists():
                pair = json.loads(pair_path.read_text())
            else:
                now = datetime.now(timezone.utc)
                # The key and certificate are a pure function of the CA key and the durable worker record,
                # so a controller restart re-derives identical material and no worker key is ever persisted
                # outside the controller's process (the Azure Files share is unavailable under org policy).
                start = datetime.fromtimestamp(int(worker["created"]), timezone.utc)
                end = min(start + timedelta(days=self.days), self.ca_cert.not_valid_after_utc)
                if not self.ca_cert.not_valid_before_utc <= now < end:
                    raise ValueError("Server CA or worker certificate window is expired/not yet valid")
                key = derived_worker_key(self.ca_key, wid, worker["lifetime"])
                serial = int.from_bytes(hashlib.sha256(b"pool-worker-serial\0" + wid.encode() + worker["lifetime"].encode()).digest()[:19], "big") | 1
                cert = (x509.CertificateBuilder()
                        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, wid)]))
                        .issuer_name(self.ca_cert.subject).public_key(key.public_key())
                        .serial_number(serial).not_valid_before(start - timedelta(minutes=1))
                        .not_valid_after(end).add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
                        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
                        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), False)
                        .sign(self.ca_key, hashes.SHA256()))
                pair = {"key": key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                 serialization.NoEncryption()).decode(),
                        "cert": cert.public_bytes(serialization.Encoding.PEM).decode()}
                atomic_json(pair_path, pair)
            template = worker_template(self.images, self.pull)
            values = dict(name=self.prefix + "-" + wid, location=self.location, subnetId=self.subnet,
                          image=self.images[0], workerId=wid, sessionId=worker["lifetime"],
                          workerHostname=hostname, maaEndpoint=self.maa, tlsCert=pair["cert"],
                          tlsKey=pair["key"], clientCa=self.client_ca)
            params = {"parameters": {k: {"value": v} for k, v in values.items()}}
            template_path, params_path = folder / "worker.json", folder / "parameters.json"
            confcom_path = folder / "confcom-parameters.json"
            atomic_json(template_path, template)
            atomic_json(params_path, params)
            # Omitting secure PEM values makes confcom emit wildcards; supplying them pins the private key.
            atomic_json(confcom_path, {"parameters": {k: v for k, v in params["parameters"].items()
                                                     if k not in ("tlsCert", "tlsKey")}})
            self.runner(template_path, confcom_path)
            confcom_path.unlink()
            template = json.loads(template_path.read_text(encoding="utf-8-sig"))
            # Existing finalizer pins runtime config; identity fields are control plane.
            finalization_copy = copy.deepcopy(template)
            finalization_copy["resources"][0].pop("identity", None)
            finalization_copy["resources"][0]["properties"].pop("imageRegistryCredentials", None)
            digest = finalize(finalization_copy, params)
            group = template["resources"][0]
            group["properties"]["confidentialComputeProperties"] = finalization_copy["resources"][0]["properties"]["confidentialComputeProperties"]
            def resolve(value):
                if isinstance(value, dict):
                    return {k: resolve(v) for k, v in value.items()}
                if isinstance(value, list):
                    return [resolve(v) for v in value]
                if isinstance(value, str):
                    match = re.fullmatch(r"\[parameters\('([^']+)'\)\]", value)
                    if match:
                        return values[match[1]]
                return value
            result = {"group": resolve(group), "server_cert": pair["cert"], "policy_hash": digest}
            atomic_json(material_path, result)
            return result

    def forget(self, worker):
        wid = worker["id"]
        if not re.fullmatch(r"[0-9a-f]{32}", wid):
            raise ValueError("Invalid worker identity")
        with file_lock(self.root / "locks" / (wid + ".lock")):
            folder = self.root / wid
            tombstone = self.root / "retired" / (wid + ".json")
            tombstone.parent.mkdir(exist_ok=True, mode=0o700)
            if tombstone.exists() and json.loads(tombstone.read_text())["lifetime"] != worker["lifetime"]:
                raise PermissionError("Retirement lifetime mismatch")
            if folder.exists():
                binding = folder / "binding.json"
                if binding.exists() and json.loads(binding.read_text())["lifetime"] != worker["lifetime"]:
                    raise PermissionError("Retirement lifetime mismatch")
            # Persist tombstone before erasing keys so a crash cannot permit reissuance.
            atomic_json(tombstone, {"lifetime": worker["lifetime"]})
            if folder.exists():
                shutil.rmtree(folder)


def environment():
    e = os.environ
    if e.get("POOL_MATERIAL_ACL_CONFIRMED") != "controller-only":
        raise ValueError("Controller-only persistent material volume ACL approval required")
    pull = pull_configuration(e.get("POOL_PULL_IDENTITY"), e.get("POOL_REGISTRY_SERVER"),
                              e.get("POOL_ALLOW_PULL_RUNTIME_IDENTITY"),
                              e.get("POOL_PULL_ROLE_ATTESTED"))
    return MaterialProvider(
        e["POOL_MATERIAL_ROOT"], e["POOL_CA_CERT"], e["POOL_CA_KEY"], e["POOL_GATEWAY_CLIENT_CA"],
        e["POOL_WORKER_IMAGE"], e["POOL_SIDECAR_IMAGE"], e["POOL_RESOURCE_PREFIX"],
        e["POOL_LOCATION"], e["POOL_SUBNET_ID"], e["POOL_MAA_ISSUER"],
        int(e["POOL_CERT_DAYS"]), int(e["POOL_CONFCOM_TIMEOUT"]), e["POOL_AZ_EXECUTABLE"], pull,
        policy_base=e.get("POOL_POLICY_BASE"))
