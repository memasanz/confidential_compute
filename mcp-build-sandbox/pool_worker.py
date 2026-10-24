"""Single-group routing guards, not isolation from code executing in that group."""

import base64
import hashlib
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def canonical_challenge(worker_id, session_id, nonce, public_key_sha256, session_public_key):
    if not all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identity) for identity in (worker_id, session_id)):
        raise ValueError("Explicit worker/session identities required")
    if not re.fullmatch(r"[0-9a-f]{64}", public_key_sha256):
        raise ValueError("Validated TLS public key SHA256 required")
    if not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", nonce):
        raise ValueError("Provide a fresh base64url nonce containing at least 32 random bytes")
    if not isinstance(session_public_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", session_public_key):
        raise ValueError("Worker session public key (X25519, base64url) required")
    return json.dumps(
        {"version": 2, "worker_id": worker_id, "session_id": session_id, "nonce": nonce,
         "tls_spki_sha256": public_key_sha256, "session_public_key": session_public_key},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")


# --- per-session key release and encrypted channel ------------------------------
# The worker generates an X25519 key pair in TEE memory. Attestation evidence commits to the
# public key (see canonical_challenge). After verifying fresh evidence, the gateway generates a
# random 256-bit session key in its own memory and wraps it to that public key. Every later
# request and response is an AES-256-GCM envelope under keys derived from the session key, so
# transport keys alone never reveal session content. Keys are never stored, logged or persisted.

def b64e(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64d(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def new_worker_keypair():
    private = X25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return private, b64e(public)


def new_session_key():
    return os.urandom(32)


def _wrap_key(shared, context):
    return HKDF(hashes.SHA256(), 32, None, b"session-key-wrap|" + context).derive(shared)


def wrap_session_key(worker_public_key, session_key, context):
    ephemeral = X25519PrivateKey.generate()
    shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(b64d(worker_public_key)))
    nonce = os.urandom(12)
    ciphertext = AESGCM(_wrap_key(shared, context)).encrypt(nonce, session_key, context)
    ephemeral_public = ephemeral.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return json.dumps({"epk": b64e(ephemeral_public), "nonce": b64e(nonce), "ct": b64e(ciphertext)})


def unwrap_session_key(private_key, wrapped, context):
    message = json.loads(wrapped)
    shared = private_key.exchange(X25519PublicKey.from_public_bytes(b64d(message["epk"])))
    key = AESGCM(_wrap_key(shared, context)).decrypt(b64d(message["nonce"]), b64d(message["ct"]), context)
    if len(key) != 32:
        raise ValueError("Session key must be 256 bits")
    return key


class SessionChannel:
    """Counter-based AES-256-GCM channel; one request in flight, strictly increasing counter.

    A bad tag, wrong counter or replayed envelope raises and changes nothing. The gateway retires
    the whole session on any failure, so there is no resynchronisation or resumption.
    """

    def __init__(self, session_key, context, tls_spki=""):
        if len(session_key) != 32:
            raise ValueError("Session key must be 256 bits")
        keys = HKDF(hashes.SHA256(), 64, None, b"session-channel|" + context).derive(session_key)
        self.request_key, self.response_key = AESGCM(keys[:32]), AESGCM(keys[32:])
        # The gateway side records the attested TLS key so later connections can be pinned to it.
        self.context, self.counter, self.tls_spki = context, 0, tls_spki

    def _seal(self, aead, label, counter, message):
        nonce = counter.to_bytes(12, "big")
        aad = self.context + b"|" + label + b"|" + str(counter).encode()
        body = aead.encrypt(nonce, json.dumps(message).encode("utf-8"), aad)
        return json.dumps({"n": counter, "ct": b64e(body)})

    def _open(self, aead, label, envelope, expected):
        message = json.loads(envelope)
        counter = message["n"]
        if type(counter) is not int or counter != expected:
            raise PermissionError("Unexpected channel counter")
        aad = self.context + b"|" + label + b"|" + str(counter).encode()
        return json.loads(aead.decrypt(counter.to_bytes(12, "big"), b64d(message["ct"]), aad))

    # Gateway side
    def seal_request(self, message):
        self.counter += 1
        return self._seal(self.request_key, b"request", self.counter, message)

    def open_response(self, envelope):
        return self._open(self.response_key, b"response", envelope, self.counter)

    # Worker side
    def open_request(self, envelope):
        message = self._open(self.request_key, b"request", envelope, self.counter + 1)
        self.counter += 1
        return message

    def seal_response(self, message):
        return self._seal(self.response_key, b"response", self.counter, message)


def public_key_hash(cert_pem):
    cert = x509.load_pem_x509_certificate(cert_pem)
    return hashlib.sha256(cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )).hexdigest()


def new_tls_identity(hostname):
    """Create this worker's TLS key and self-signed certificate. Returns (cert_pem, key_pem).

    The key is generated inside the TEE and never leaves it. Clients trust it only because
    attestation evidence commits to its fingerprint, not because a CA signed it.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname.split(".")[0])])
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=2))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), False)
            .sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()))


def validate_client_trust(cafile):
    """The worker accepts only gateway client certificates issued by this public CA chain."""
    ca_pem = Path(cafile).read_bytes()
    if b"PRIVATE KEY" in ca_pem:
        raise ValueError("Workers must receive only the PUBLIC gateway client CA trust chain")
    roots = x509.load_pem_x509_certificates(ca_pem)
    if not roots or any(not c.extensions.get_extension_for_class(x509.BasicConstraints).value.ca for c in roots):
        raise ValueError("Client trust must contain CA certificates only")
    now = datetime.now(timezone.utc)
    if any(not c.not_valid_before_utc <= now < c.not_valid_after_utc for c in roots):
        raise ValueError("Gateway client trust CA is not currently valid")


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
