"""Generate the pool PKI for a pilot into an ignored directory (default .azure\\pool-pilot\\pki).

Creates a worker-server CA (cert+key, controller only), a dedicated gateway-client CA (public cert is
all that workers and the controller need; its key is written for rotation only and is not deployed),
and one gateway client certificate/key (clientAuth). Never overwrites existing files.
"""

import argparse
import sys
from pathlib import Path

from cryptography.x509.oid import ExtendedKeyUsageOID

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prepare import issue, pem, write_private


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
