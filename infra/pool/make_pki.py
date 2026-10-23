"""Generate the pool PKI for a pilot into an ignored directory (default .azure\\pool-pilot\\pki).

Creates a worker-server CA (cert+key, controller only), a dedicated gateway-client CA (public cert is
all that workers and the controller need; its key is written for rotation only and is not deployed),
and one gateway client certificate/key (clientAuth). Never overwrites existing files.
"""

import argparse
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID
from cryptography.x509.oid import NameOID


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".azure") / "pool-pilot" / "pki")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    client_ca = issue("pool-gateway-client-ca", days=args.days)
    server_ca = issue("pool-worker-server-ca", days=args.days)
    for name, pair in (("client-ca", client_ca), ("server-ca", server_ca),
                       ("gateway-client", issue("pool-gateway", client_ca, ExtendedKeyUsageOID.CLIENT_AUTH,
                                                days=args.days))):
        key, cert = pem(pair)
        write_private(args.output / f"{name}.key", key)
        write_private(args.output / f"{name}.pem", cert)
    print("PKI written to", args.output)


if __name__ == "__main__":
    main()
