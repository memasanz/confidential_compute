"""Two-worker mTLS/MAA gate. No provisioning, credentials release, or controller.

Run from repository root with --manifest .azure\\pool-feasibility\\manifest.json,
two --policy-hash FINAL_SHA256 values and --minimum-svn JSON containing explicit
bootloader/tee/snpfw/microcode/guest/uvm_guest minima. Authoritative DNS must resolve each
worker hostname to its own ACI group. This does not update DNS.
--exercise-lifecycle runs a trivial check AFTER verification, then retires both
workspaces; Azure group destruction is still required externally.
"""

import argparse
import asyncio
import json
import secrets
import ssl
import sys
import time
from pathlib import Path

import httpx
from jwt import PyJWKClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp-build-sandbox"))
from pool_worker import canonical_challenge, public_key_hash, verify_maa_token


async def verify_worker(root, manifest, worker, policy_hash, minimum_svn, allow_smt, exercise):
    context = ssl.create_default_context(cafile=str(root / manifest["server_ca"]))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(root / manifest["gateway_cert"]), str(root / manifest["gateway_key"]))
    expected_spki = public_key_hash((root / worker["cert"]).read_bytes())

    async def check_peer(response):
        stream = response.extensions.get("network_stream")
        peer = stream.get_extra_info("ssl_object") if stream else None
        if peer is None:
            raise ValueError("Cannot obtain actual TLS peer certificate; refusing verification")
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        pem = x509.load_der_x509_certificate(peer.getpeercert(binary_form=True)).public_bytes(serialization.Encoding.PEM)
        if public_key_hash(pem) != expected_spki:
            raise ValueError("Actual TLS peer key differs from expected attested worker key")

    url = f"https://{worker['hostname']}:8000/mcp"
    async with (
        httpx.AsyncClient(verify=context, timeout=httpx.Timeout(30, read=300),
                          trust_env=False, event_hooks={"response": [check_peer]}) as client,
        streamable_http_client(url, http_client=client) as (read, write, _),
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            nonce, requested_at = secrets.token_urlsafe(32), time.time()
            challenge = canonical_challenge(worker["worker_id"], worker["session_id"], nonce, expected_spki)
            result = await session.call_tool("get_attestation", {"nonce": nonce})
            if result.isError:
                raise RuntimeError("Worker attestation failed; no activation permitted")
            token = json.loads(result.content[0].text)["token"]
            parameters = json.loads((root / worker["parameters"]).read_text())
            issuer = "https://" + parameters["parameters"]["maaEndpoint"]["value"]
            # Pinned issuer URL, not token-controlled jku or x5u.
            key = await asyncio.to_thread(
                PyJWKClient(issuer + "/certs").get_signing_key_from_jwt, token)
            verify_maa_token(token, issuer, key.key, challenge, policy_hash,
                             minimum_svn, requested_at, allow_smt=allow_smt)
            print(f"{worker['worker_id']}: fresh signed SNP evidence verified against TLS peer and FINAL policy")
            if exercise:
                async def call(name, args):
                    response = await session.call_tool(name, args)
                    if response.isError:
                        raise RuntimeError(f"{name} failed")
                    return response
                sid = worker["session_id"]
                await call("create_session", {"session_id": sid})
                try:
                    await call("run_python", {"session_id": sid, "code": "print('feasibility-only')"})
                finally:
                    await call("delete_session", {"session_id": sid})
                print(f"{worker['worker_id']}: retired; CONTROLLER MUST DESTROY GROUP")


async def main(args):
    manifest = json.loads(args.manifest.read_text())
    if len(manifest["workers"]) != 2 or len(args.policy_hash) != 2:
        raise ValueError("Exactly two workers and FINAL policy hashes required")
    if len({w["hostname"] for w in manifest["workers"]}) != 2:
        raise ValueError("Distinct authoritative worker hostnames required")
    for worker, policy_hash in zip(manifest["workers"], args.policy_hash):
        await verify_worker(args.manifest.parent, manifest, worker, policy_hash,
                            json.loads(args.minimum_svn), args.allow_smt, args.exercise_lifecycle)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--policy-hash", action="append", required=True)
    parser.add_argument("--minimum-svn", required=True)
    parser.add_argument("--allow-smt", action="store_true", help="Explicit security approval for SMT-enabled workers")
    parser.add_argument("--exercise-lifecycle", action="store_true")
    asyncio.run(main(parser.parse_args()))
