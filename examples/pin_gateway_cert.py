"""Save the pool gateway's TLS certificate and print its SHA-256 fingerprint.

The gateway sits behind the Container Apps internal ingress, which presents a self-signed
certificate. Clients trust exactly this certificate (pinning), not the public CA store.

Admins (inside the VNet/VPN) run this once and publish the fingerprint over a trusted channel.
Users run it with --expect <fingerprint>; it refuses to write the file if the fingerprint differs.

    python pin_gateway_cert.py https://pool-gateway.<env-domain>/mcp --out gateway-cert.pem
    python pin_gateway_cert.py https://pool-gateway.<env-domain>/mcp --expect <sha256> --out gateway-cert.pem
"""

import argparse
import hashlib
import ssl
import sys
from pathlib import Path
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--out", type=Path, default=Path("gateway-cert.pem"))
    parser.add_argument("--expect", help="Fingerprint published by the admin (hex, colons optional)")
    args = parser.parse_args()

    parts = urlsplit(args.url)
    pem = ssl.get_server_certificate((parts.hostname, parts.port or 443), timeout=15)
    fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
    print("sha256:", fingerprint)

    if args.expect and args.expect.replace(":", "").lower() != fingerprint:
        sys.exit("Fingerprint mismatch: not writing the certificate. Do not connect.")
    args.out.write_text(pem)
    print("wrote", args.out)


if __name__ == "__main__":
    main()
