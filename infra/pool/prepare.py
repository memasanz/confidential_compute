"""LOCAL ONLY two-worker fixture preparation. Never calls Azure or builds images.

Run from the repository root:
 .venv\\Scripts\\python infra\\pool\\prepare.py --hostname worker-a.example.test
   --hostname worker-b.example.test --image REGISTRY/IMAGE@sha256:DIGEST
Output secrets remain under ignored .azure\\pool-feasibility. Replace the public
subnet placeholder locally before policy generation. No deployment.
The template contains no registry credentials or managed identity: the existing
image must be pull-accessible without embedding a fleet credential.
"""

import argparse
import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def issue(name, issuer=None, eku=None, hostname=None, days=7):
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(timezone.utc)
    builder = (x509.CertificateBuilder().subject_name(subject)
               .issuer_name(issuer[1].subject if issuer else subject)
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now - timedelta(minutes=1))
               .not_valid_after(now + timedelta(days=days))
               .add_extension(x509.BasicConstraints(ca=issuer is None, path_length=0 if issuer is None else None), True)
               .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                           key_encipherment=issuer is not None, data_encipherment=False,
                                           key_agreement=False, key_cert_sign=issuer is None,
                                           crl_sign=issuer is None, encipher_only=False, decipher_only=False), True))
    if eku:
        builder = builder.add_extension(x509.ExtendedKeyUsage([eku]), True)
    if hostname:
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), False)
    return key, builder.sign(issuer[0] if issuer else key, hashes.SHA256())


def pem(pair):
    return (pair[0].private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()).decode(),
            pair[1].public_bytes(serialization.Encoding.PEM).decode())


def write_private(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content)


def prepare(output, hostnames, image):
    if len(hostnames) != 2 or len(set(hostnames)) != 2:
        raise ValueError("Exactly two distinct authoritative DNS hostnames required")
    if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", h) or "." not in h for h in hostnames):
        raise ValueError("Provide DNS hostnames, not IP addresses or URLs")
    if not re.fullmatch(r".+@sha256:[0-9a-f]{64}", image):
        raise ValueError("Use a pre-existing image pinned by SHA256; this tool does not build/push")
    output.mkdir(parents=True, exist_ok=False)
    client_ca, server_ca = issue("dedicated-gateway-client-ca"), issue("worker-server-ca")
    for name, pair in (("client-ca", client_ca), ("server-ca", server_ca),
                       ("gateway-client", issue("gateway", client_ca, ExtendedKeyUsageOID.CLIENT_AUTH))):
        key, cert = pem(pair)
        write_private(output / f"{name}.key", key)
        write_private(output / f"{name}.pem", cert)
    manifest = {"server_ca": "server-ca.pem", "gateway_cert": "gateway-client.pem",
                "gateway_key": "gateway-client.key", "workers": []}
    for index, hostname in enumerate(hostnames, 1):
        worker_id, session_id = f"feasibility-worker-{index}", f"feasibility-session-{index}"
        key, cert = pem(issue(worker_id, server_ca, ExtendedKeyUsageOID.SERVER_AUTH, hostname))
        cert += pem(server_ca)[1]
        write_private(output / f"worker-{index}.key", key)
        write_private(output / f"worker-{index}.pem", cert)
        values = dict(name=worker_id, location="eastus2", subnetId="REPLACE_WITH_EXISTING_POOL_SUBNET_ID",
                      image=image, workerId=worker_id,
                      sessionId=session_id, workerHostname=hostname,
                      maaEndpoint="sharedeus2.eus2.attest.azure.net",
                      tlsCert=cert, tlsKey=key, clientCa=pem(client_ca)[1])
        parameters = {"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
                      "contentVersion": "1.0.0.0",
                      "parameters": {k: {"value": v} for k, v in values.items()}}
        write_private(output / f"worker-{index}.parameters.json", json.dumps(parameters, indent=2))
        shutil.copyfile(Path(__file__).with_name("worker.example.json"), output / f"worker-{index}.json")
        manifest["workers"].append({"worker_id": worker_id, "session_id": session_id,
                                    "hostname": hostname, "cert": f"worker-{index}.pem",
                                    "template": f"worker-{index}.json",
                                    "parameters": f"worker-{index}.parameters.json"})
    write_private(output / "manifest.json", json.dumps(manifest, indent=2))
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hostname", action="append", required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    prepare(Path(".azure") / "pool-feasibility", args.hostname, args.image)
    print("Prepared two LOCAL workers; secrets under ignored .azure\\pool-feasibility.")
    print("Restrict this directory's Windows ACL to the operator. No resources deployed.")
    print("Next gate: az confcom acipolicygen --approve-wildcards, then finalize_policy.py.")
