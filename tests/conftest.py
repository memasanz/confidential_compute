"""Explicit loopback dialing preserves DNS Host/SNI and full TLS verification."""

import ssl
import socket

import httpx
import pytest


@pytest.fixture
def hostile_loopback_environment(monkeypatch):
    """Prove loopback tests need neither name resolution nor ambient proxies."""
    original = socket.getaddrinfo
    def numeric_only(host, port, *args, **kwargs):
        if host not in ("127.0.0.1", b"127.0.0.1", "::1", b"::1"):
            raise socket.gaierror(11001, "Test forbids external DNS")
        return original(host, port, *args, **kwargs)
    monkeypatch.setattr(socket, "getaddrinfo", numeric_only)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://unresolvable-proxy.invalid:3128")
    monkeypatch.setenv("NO_PROXY", "")


def pinned_request(request):
    if request.url.scheme != "https":
        raise ValueError("TLS required")
    return httpx.Request(
        request.method, request.url.copy_with(host="127.0.0.1"),
        headers=request.headers, stream=request.stream,
        extensions={**request.extensions, "sni_hostname": request.url.host})


class LoopbackTransport(httpx.HTTPTransport):
    def __init__(self, context):
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        super().__init__(verify=context, trust_env=False, retries=0)

    def handle_request(self, request):
        return super().handle_request(pinned_request(request))


class AsyncLoopbackTransport(httpx.AsyncHTTPTransport):
    def __init__(self, context):
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        super().__init__(verify=context, trust_env=False, retries=0)

    async def handle_async_request(self, request):
        return await super().handle_async_request(pinned_request(request))
