"""mTLS MCP transport. No retry of application calls, including activation."""

import asyncio
import ipaddress
import json
import re
import secrets
import socket
import ssl
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from jwt import PyJWKClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp-build-sandbox"))
from pool_worker import (SessionChannel, canonical_challenge, new_session_key, public_key_hash,
                         verify_maa_token, wrap_session_key)


class PinnedAddressTransport(httpx.AsyncHTTPTransport):
    """Connect to the authoritative IP for the worker's DNS name.

    Workers present a self-signed certificate, so this transport cannot verify it. WorkerTransport
    checks the actual peer key on every response instead and trusts it only through attestation.
    """
    def __init__(self, context, hostname, address, port):
        super().__init__(verify=context, trust_env=False, retries=0)
        self.hostname, self.address, self.port = hostname, str(ipaddress.ip_address(address)), port

    async def handle_async_request(self, request):
        if (request.url.scheme != "https" or request.url.host != self.hostname
                or (request.url.port or 443) != self.port):
            raise ValueError("Request outside pinned worker origin")
        pinned = httpx.Request(
            request.method, request.url.copy_with(host=self.address),
            headers=request.headers, stream=request.stream,
            extensions={**request.extensions, "sni_hostname": self.hostname})
        return await super().handle_async_request(pinned)


class BoundedStream(httpx.AsyncByteStream):
    def __init__(self, stream, limit):
        self.stream, self.limit = stream, limit

    async def __aiter__(self):
        count = 0
        async for chunk in self.stream:
            count += len(chunk)
            if count > self.limit:
                raise ValueError("Worker response exceeds configured bound")
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class WorkerTransport:
    def __init__(self, cert, key, dns_suffix, subnet, maa_issuer,
                 minimum_svn, limits, allow_smt=False, signing_key=None, port=8000):
        # Workers create their own TLS key inside the TEE, so there is no CA to verify against.
        # The peer key is trusted only after attestation evidence commits to its fingerprint.
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.check_hostname = False
        self.context.verify_mode = ssl.CERT_NONE
        self.context.load_cert_chain(cert, key)
        self.suffix = dns_suffix.strip(".")
        self.subnet = ipaddress.ip_network(subnet)
        if not self.subnet.is_private or not maa_issuer.startswith("https://"):
            raise ValueError("Private subnet and pinned HTTPS MAA issuer required")
        self.issuer, self.minimum_svn = maa_issuer.rstrip("/"), minimum_svn
        required = {"bootloader", "tee", "snpfw", "microcode", "guest", "uvm_guest"}
        if (set(minimum_svn) != required
                or any(type(v) is not int or v < 0 for v in minimum_svn.values())
                or type(allow_smt) is not bool):
            raise ValueError("Explicit security-approved minima and SMT setting required")
        self.limits, self.allow_smt, self.signing_key = limits, allow_smt, signing_key
        if not 0 < port <= 65535:
            raise ValueError("Invalid fixed worker port")
        self.port = port
        self.keys = PyJWKClient(self.issuer + "/certs", timeout=10)

    @asynccontextmanager
    async def connect(self, worker, pin=""):
        """Open an MCP session; yield (session, peer) where peer["spki"] is the worker's TLS key.

        Every response must come from the same TLS key: the pin if one is given, otherwise the
        first key seen. Callers must verify attestation against peer["spki"] before trusting it.
        """
        r = worker["routing"]
        host, address = r["hostname"], ipaddress.ip_address(r["ip"])
        if (not re.fullmatch(r"[a-z0-9-]{1,63}", host.split(".")[0])
                or host != worker["id"] + "." + self.suffix
                or address not in self.subnet):
            raise ValueError("Unapproved worker mapping")
        addresses = None
        deadline = time.monotonic() + 90
        while addresses is None:
            try:
                addresses = await asyncio.to_thread(socket.getaddrinfo, host, self.port,
                                                    type=socket.SOCK_STREAM)
            except socket.gaierror:
                # A freshly registered private DNS record can take a while to become visible to the resolver.
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(3)
        if not addresses or {a[4][0] for a in addresses} != {str(address)}:
            raise ValueError("DNS does not match authoritative private IP")
        peer_key = {"spki": pin}

        async def check_peer(response):
            if response.headers.get("content-encoding", "identity") != "identity":
                raise ValueError("Compressed worker responses are not accepted")
            stream = response.extensions.get("network_stream")
            tls = stream.get_extra_info("ssl_object") if stream else None
            if tls is None:
                raise ValueError("Actual TLS peer certificate unavailable")
            pem = x509.load_der_x509_certificate(tls.getpeercert(binary_form=True)).public_bytes(
                serialization.Encoding.PEM)
            spki = public_key_hash(pem)
            if peer_key["spki"] and spki != peer_key["spki"]:
                raise ValueError("TLS peer differs from the expected attested key")
            peer_key["spki"] = spki
            # Encrypted envelopes are base64 of the plaintext reply, so allow for expansion.
            response.stream = BoundedStream(response.stream, self.limits.result_bytes * 2 + 4096)

        async with httpx.AsyncClient(
                transport=PinnedAddressTransport(self.context, host, address, self.port),
                trust_env=False, follow_redirects=False,
                headers={"Accept-Encoding": "identity"},
                timeout=httpx.Timeout(self.limits.operation, connect=10),
                event_hooks={"response": [check_peer]}) as client:
            async with streamable_http_client(
                    f"https://{host}:{self.port}/mcp", http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session, peer_key

    async def verify(self, session, peer_key, worker):
        """Verify fresh evidence that commits to the TLS key seen on this connection.

        Returns the attested worker session public key. A different key on the wire, such as a
        man in the middle, produces a challenge the hardware never signed, so verification fails.
        """
        nonce, requested = secrets.token_urlsafe(32), time.time()
        result = await session.call_tool("get_attestation", {"nonce": nonce})
        if result.isError:
            raise ValueError("Attestation failed")
        evidence = json.loads(result.content[0].text)
        token, public_key = evidence["token"], evidence["session_public_key"]
        challenge = canonical_challenge(worker["id"], worker["lifetime"], nonce,
                                        peer_key["spki"], public_key)
        key = self.signing_key
        if key is None:
            key = (await asyncio.to_thread(self.keys.get_signing_key_from_jwt, token)).key
        verify_maa_token(token, self.issuer, key, challenge,
                         worker["routing"]["policy_hash"], self.minimum_svn,
                         requested, allow_smt=self.allow_smt)
        return public_key

    async def ready(self, worker):
        async with self.connect(worker) as (session, peer_key):
            await self.verify(session, peer_key, worker)

    async def activate(self, worker):
        # Fresh verification and activation on the same authenticated MCP connection.
        async with self.connect(worker) as (session, peer_key):
            await self.verify(session, peer_key, worker)
            result = await session.call_tool("create_session", {"session_id": worker["lifetime"]})
            if result.isError:
                raise ValueError("Activation failed")

    async def establish(self, worker):
        """Release a new random session key only after fresh attestation, on the same connection.

        The key exists only in this process and in the attested worker; it is never stored. It is
        wrapped to the attested public key, so it is useless to anyone else even on the wire.
        """
        context = f"{worker['id']}:{worker['lifetime']}".encode("ascii")
        async with self.connect(worker) as (session, peer_key):
            public_key = await self.verify(session, peer_key, worker)
            session_key = new_session_key()
            result = await session.call_tool(
                "establish_session_key",
                {"wrapped_key": wrap_session_key(public_key, session_key, context)})
            if result.isError:
                raise ValueError("Session key release failed")
        return SessionChannel(session_key, context, peer_key["spki"])

    async def invoke(self, worker, channel, name, arguments):
        async with self.connect(worker, channel.tls_spki) as (session, _):
            result = await session.call_tool(
                "invoke", {"envelope": channel.seal_request({"tool": name, "arguments": arguments})})
            if result.isError:
                raise ValueError("Encrypted call rejected")
            reply = channel.open_response(result.content[0].text)
        return CallToolResult(content=[TextContent(type="text", text=reply["text"])],
                              isError=reply["error"])

    async def call(self, worker, name, arguments):
        # Plain calls are limited to evidence retrieval, which the caller verifies itself.
        async with self.connect(worker) as (session, _):
            return await session.call_tool(name, arguments)
