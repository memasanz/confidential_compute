"""Single-group routing guards, not isolation from code executing in that group."""

import base64
import hashlib
import json
import os
import re
import ssl
import threading
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID


def canonical_challenge(worker_id, session_id, nonce, public_key_sha256):
    if not all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identity) for identity in (worker_id, session_id)):
        raise ValueError("Explicit worker/session identities required")
    if not re.fullmatch(r"[0-9a-f]{64}", public_key_sha256):
        raise ValueError("Validated TLS public key SHA256 required")
    if not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", nonce):
        raise ValueError("Provide a fresh base64url nonce containing at least 32 random bytes")
    return json.dumps(
        {"version": 1, "worker_id": worker_id, "session_id": session_id,
         "nonce": nonce, "tls_spki_sha256": public_key_sha256},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")


def public_key_hash(cert_pem):
    cert = x509.load_pem_x509_certificate(cert_pem)
    return hashlib.sha256(cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )).hexdigest()


def validate_tls(certfile, keyfile, cafile, hostname):
    if not all((certfile, keyfile, cafile, hostname)):
        raise ValueError("Pooled mode requires TLS certificate/key, dedicated client CA and worker DNS hostname")
    cert = x509.load_pem_x509_certificate(Path(certfile).read_bytes())
    now = datetime.now(timezone.utc)
    if not cert.not_valid_before_utc <= now < cert.not_valid_after_utc:
        raise ValueError("Worker TLS certificate is not currently valid")
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    if ExtendedKeyUsageOID.SERVER_AUTH not in eku or ExtendedKeyUsageOID.CLIENT_AUTH in eku:
        raise ValueError("Worker certificate must be serverAuth only")
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    if hostname not in names.get_values_for_type(x509.DNSName):
        raise ValueError("Worker hostname must exactly match a certificate DNS SAN")
    ca_pem = Path(cafile).read_bytes()
    if b"PRIVATE KEY" in ca_pem:
        raise ValueError("Workers must receive only the PUBLIC gateway client CA trust chain")
    roots = x509.load_pem_x509_certificates(ca_pem)
    if not roots or any(not c.extensions.get_extension_for_class(x509.BasicConstraints).value.ca for c in roots):
        raise ValueError("Client trust must contain CA certificates only")
    if any(c.subject == cert.issuer for c in roots):
        raise ValueError("Client issuing CA must be separate from the worker server issuing CA")
    if any(not c.not_valid_before_utc <= now < c.not_valid_after_utc for c in roots):
        raise ValueError("Gateway client trust CA is not currently valid")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile, keyfile)
    context.load_verify_locations(cafile=cafile)
    context.verify_mode = ssl.CERT_REQUIRED
    return public_key_hash(Path(certfile).read_bytes())


class LifetimeSession:
    def __init__(self, workspace, worker_id, session_id):
        for value in (worker_id, session_id):
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
                raise ValueError("Pooled worker/session IDs must be explicit safe identifiers")
        self.path = workspace / ".pool-lifetime.json"
        self.worker_id, self.session_id = worker_id, session_id
        self.lock = threading.RLock()
        if not self.path.exists():
            if any((workspace / "sessions").iterdir()):
                raise ValueError("Cannot activate pooled mode over existing legacy workspaces")
            with self.path.open("x", encoding="utf-8") as stream:
                json.dump(self._record("ready"), stream)
                stream.flush()
                os.fsync(stream.fileno())
        self._read()

    def _record(self, status):
        return {"worker_id": self.worker_id, "session_id": self.session_id, "status": status}

    def _read(self):
        record = json.loads(self.path.read_text(encoding="utf-8"))
        if record not in [self._record(s) for s in ("ready", "active", "retired")]:
            raise RuntimeError("Invalid or mismatched lifetime state; destroy this group")
        return record["status"]

    def require(self, session_id=None, active=True):
        with self.lock:
            status = self._read()
            if status == "retired":
                raise PermissionError("Worker retired; controller must destroy the Azure group")
            if session_id is not None and session_id != self.session_id:
                raise PermissionError("Wrong lifetime session")
            if active and status != "active":
                raise PermissionError("Lifetime session is not active")
            return status

    def transition(self, status):
        with self.lock:
            previous = self.require(active=False)
            if (previous, status) not in (("ready", "active"), ("active", "retired")):
                raise PermissionError("One lifetime session only; no recycling")
            replacement = self.path.with_suffix(".new")
            with replacement.open("w", encoding="utf-8") as stream:
                json.dump(self._record(status), stream)
                stream.flush()
                os.fsync(stream.fileno())
            replacement.replace(self.path)


def verify_maa_token(token, issuer, signing_key, challenge, hostdata, minimum_svn,
                     requested_at, max_age=120, allow_smt=False):
    """Verify signed MAA claims; missing claims block, never silently downgrade.

    Formats: Learn attestation-token-examples; SHA256 + 32 zero bytes:
    confidential-sidecar-containers/pkg/attest/attest.go GenerateMAAReportData.
    signing_key MUST be obtained from the configured issuer's HTTPS /certs,
    never from the untrusted JWT jku.
    """
    import time
    import jwt

    claims = jwt.decode(token, signing_key, algorithms=["RS256"], issuer=issuer,
                        options={"verify_aud": False,
                                 "require": ["exp", "iat", "nbf", "iss"]})
    now = time.time()
    if type(allow_smt) is not bool or not 0 < max_age <= 300 or requested_at > now or now - requested_at > max_age:
        raise ValueError("Challenge expired or invalid")
    if claims["iat"] < requested_at - 2 or now - claims["iat"] > max_age:
        raise ValueError("Stale attestation")
    expected_report = (hashlib.sha256(challenge).digest() + bytes(32)).hex()
    checks = {
        "x-ms-attestation-type": "sevsnpvm",
        "x-ms-compliance-status": "azure-compliant-uvm",
        "x-ms-sevsnpvm-hostdata": hostdata,
        "x-ms-sevsnpvm-reportdata": expected_report,
        "x-ms-sevsnpvm-is-debuggable": False,
        "x-ms-sevsnpvm-migration-allowed": False,
        "x-ms-sevsnpvm-vmpl": 0,
        "x-ms-sevsnpvm-smt-allowed": allow_smt,
        "x-ms-runtime": json.loads(challenge),
    }
    if not re.fullmatch(r"[0-9a-f]{64}", hostdata):
        raise ValueError("Expected FINAL CCE SHA256 as lowercase hex")
    for name, expected in checks.items():
        actual = claims.get(name)
        if type(actual) is not type(expected) or actual != expected:
            raise ValueError(f"Missing or unacceptable attestation claim: {name}")
    required_svn = {"bootloader", "tee", "snpfw", "microcode", "guest", "uvm_guest"}
    if set(minimum_svn) != required_svn:
        raise ValueError("Explicit minimum SVN required for all security components")
    for component, minimum in minimum_svn.items():
        if component == "uvm_guest":
            continue
        name = f"x-ms-sevsnpvm-{component}{'svn' if component == 'guest' else '-svn'}"
        value = claims.get(name)
        if type(minimum) is not int or minimum < 0 or type(value) is not int or value < minimum:
            raise ValueError(f"Missing or insufficient SVN: {name}")
    # ACI-specific endorsed utility VM is required: hostdata alone is not proof
    # that arbitrary SNP guest code actually enforces the CCE policy.
    # Formats: Learn /azure/container-instances/confidential-containers-attestation-concepts.
    measurement = claims.get("x-ms-sevsnpvm-launchmeasurement")
    endorsement = claims.get("x-ms-sevsnpvm-uvm-endorsement")
    if not isinstance(measurement, str) or not re.fullmatch(r"[0-9a-f]{96}", measurement):
        raise ValueError("Missing valid UVM launch measurement")
    if not isinstance(endorsement, dict) or endorsement.get("x-ms-sevsnpvm-launchmeasurement") != measurement:
        raise ValueError("Missing matching verified UVM endorsement")
    endorsed_svn = endorsement.get("x-ms-sevsnpvm-guestsvn")
    minimum = minimum_svn["uvm_guest"]
    if (not isinstance(endorsed_svn, str) or not re.fullmatch(r"[0-9]+", endorsed_svn)
            or type(minimum) is not int or minimum < 0 or int(endorsed_svn) < minimum):
        raise ValueError("Missing or insufficient endorsed UVM guest SVN")
    return claims
