import base64
import importlib.util
import json
import secrets
import shutil
import socket
import ssl
import sys
import threading
import time
import uuid
from pathlib import Path

import jwt
import pytest
from cryptography.x509.oid import ExtendedKeyUsageOID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp-build-sandbox"))
sys.path.insert(0, str(ROOT / "infra" / "pool"))
from pool_worker import LifetimeSession, canonical_challenge, public_key_hash, validate_tls, verify_maa_token
from make_pki import issue, pem, write_private
from finalize_policy import finalize


@pytest.fixture
def local():
    path = ROOT / ".azure" / "pool-feasibility-tests" / uuid.uuid4().hex
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path)


def load_server(monkeypatch, local):
    monkeypatch.setenv("SANDBOX_WORKSPACE", str(local))
    monkeypatch.setenv("MCP_WORKER_MODE", "pooled")
    monkeypatch.setenv("MCP_WORKER_ID", "worker-a")
    monkeypatch.setenv("MCP_SESSION_ID", "session-a")
    spec = importlib.util.spec_from_file_location("worker_test_" + uuid.uuid4().hex, ROOT / "mcp-build-sandbox" / "server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    server.current_caller.set("gateway-mtls")
    monkeypatch.setattr(server.venv, "create", lambda *a, **k: None)
    return server


TOOLS = {
    "write_file": {"path": "x", "content": "x"},
    "read_file": {"path": "x"}, "list_dir": {},
    "upload_file": {"path": "x", "content_base64": "eA=="},
    "download_file": {"path": "x"}, "run_python": {"code": "print(1)"},
    "run_command": {"command": "echo x"}, "pip_install": {"packages": "x"},
    "run_tests": {}, "git_clone": {"repo_url": "https://example.invalid/x"},
    "delete_session": {},
}


@pytest.mark.parametrize("name,args", TOOLS.items())
def test_all_tools_guard_lifetime(monkeypatch, local, name, args):
    server = load_server(monkeypatch, local)
    tool = getattr(server, name)
    with pytest.raises(PermissionError):
        tool(session_id="session-a", **args)
    server.create_session("session-a")
    with pytest.raises(PermissionError):
        tool(session_id="other", **args)
    server.delete_session("session-a")
    with pytest.raises(PermissionError):
        tool(session_id="session-a", **args)


def test_retirement_persists_and_listing_attestation_block(monkeypatch, local):
    server = load_server(monkeypatch, local)
    assert server.list_sessions() == "(none)"
    assert server.create_session() == "session-a"
    assert server.list_sessions() == "session-a"
    with pytest.raises(PermissionError):
        server.create_session()
    assert "controller" in server.delete_session("session-a")
    server = load_server(monkeypatch, local)
    for call in (server.create_session, server.list_sessions,
                 lambda: server.get_attestation(secrets.token_urlsafe(32))):
        with pytest.raises(PermissionError):
            call()


def test_authentication_and_corrupt_state(monkeypatch, local):
    server = load_server(monkeypatch, local)
    server.current_caller.set("")
    with pytest.raises(PermissionError):
        server.create_session()
    (local / ".pool-lifetime.json").write_text("{}")
    with pytest.raises(RuntimeError):
        LifetimeSession(local, "worker-a", "session-a")


def test_attestation_challenge_and_errors(monkeypatch, local):
    server = load_server(monkeypatch, local)
    server.TLS_SPKI_SHA256 = "a" * 64
    nonce = secrets.token_urlsafe(32)
    sent = []
    class Response:
        text = '{"token":"unit-fixture-not-live"}'
        def raise_for_status(self):
            pass
        def json(self):
            return json.loads(self.text)
    monkeypatch.setattr(server.httpx, "post", lambda *a, **k: sent.append(k["json"]) or Response())
    assert "unit-fixture" in server.get_attestation(nonce)
    blob = base64.b64decode(sent[0]["runtime_data"])
    assert blob == canonical_challenge("worker-a", "session-a", nonce, "a" * 64)
    with pytest.raises(ValueError):
        server.get_attestation()
    monkeypatch.setattr(Response, "text", '{"error":"no report"}')
    with pytest.raises(RuntimeError):
        server.get_attestation(nonce)


@pytest.fixture
def pki(local):
    client_ca, server_ca = issue("client-ca"), issue("server-ca")
    pairs = {"client-ca": client_ca, "server-ca": server_ca,
             "server": issue("localhost", server_ca, ExtendedKeyUsageOID.SERVER_AUTH, "localhost"),
             "gateway": issue("gateway", client_ca, ExtendedKeyUsageOID.CLIENT_AUTH),
             "wrong-eku": issue("wrong-purpose", client_ca, ExtendedKeyUsageOID.SERVER_AUTH),
             "foreign": issue("foreign", server_ca, ExtendedKeyUsageOID.CLIENT_AUTH)}
    for name, pair in pairs.items():
        key, cert = pem(pair)
        (local / f"{name}.key").write_text(key)
        (local / f"{name}.pem").write_text(cert)
    return local, pairs


def test_real_tls_client_purposes(pki):
    path, _ = pki
    validate_tls(str(path / "server.pem"), str(path / "server.key"), str(path / "client-ca.pem"), "localhost")
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(str(path / "server.pem"), str(path / "server.key"))
    server_context.load_verify_locations(str(path / "client-ca.pem"))
    server_context.verify_mode = ssl.CERT_REQUIRED
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(10)
    outcomes = []
    def serve():
        for _ in range(5):
            connection, _ = listener.accept()
            try:
                with server_context.wrap_socket(connection, server_side=True) as tls:
                    tls.sendall(b"accepted")
                    outcomes.append(True)
            except ssl.SSLError:
                connection.close()
                outcomes.append(False)
    thread = threading.Thread(target=serve)
    thread.start()
    try:
        for name in (None, "wrong-eku", "server", "foreign", "gateway"):
            context = ssl.create_default_context(cafile=str(path / "server-ca.pem"))
            if name:
                context.load_cert_chain(str(path / f"{name}.pem"), str(path / f"{name}.key"))
            try:
                with socket.create_connection(listener.getsockname(), timeout=5) as sock:
                    with context.wrap_socket(sock, server_hostname="localhost") as tls:
                        received = tls.recv(8)
                        assert (received == b"accepted") == (name == "gateway")
            except (ssl.SSLError, ConnectionResetError):
                assert name != "gateway"
    finally:
        thread.join(timeout=12)
        listener.close()
    assert not thread.is_alive()
    assert outcomes == [False, False, False, False, True]


def test_tls_configuration_fails_closed(monkeypatch, local, pki):
    path, _ = pki
    with pytest.raises(ValueError):
        validate_tls(None, None, None, "")
    with pytest.raises(ValueError):
        validate_tls(str(path / "server.pem"), str(path / "server.key"),
                     str(path / "client-ca.pem"), "wrong.test")
    with pytest.raises(ValueError):
        validate_tls(str(path / "server.pem"), str(path / "server.key"),
                     str(path / "server-ca.pem"), "localhost")
    with pytest.raises(ssl.SSLError):
        validate_tls(str(path / "server.pem"), str(path / "gateway.key"),
                     str(path / "client-ca.pem"), "localhost")
    server = load_server(monkeypatch, local)
    with pytest.raises(ValueError):
        server.transport_options()
    server.TLS_CERTFILE, server.TLS_KEYFILE = str(path / "server.pem"), str(path / "server.key")
    server.TLS_CLIENT_CAFILE, server.WORKER_HOSTNAME = str(path / "client-ca.pem"), "localhost"
    server.SKR_MAA_ENDPOINT = "test.attest.azure.net"
    options = server.transport_options()
    assert options["ssl_cert_reqs"] == ssl.CERT_REQUIRED
    assert options["proxy_headers"] is False


def test_real_uvicorn_mtls_mcp_lifecycle(monkeypatch, local, pki, hostile_loopback_environment):
    import asyncio
    import httpx
    import uvicorn
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from conftest import LoopbackTransport, AsyncLoopbackTransport

    path, _ = pki
    worker = load_server(monkeypatch, local)
    worker.TLS_CERTFILE, worker.TLS_KEYFILE = str(path / "server.pem"), str(path / "server.key")
    worker.TLS_CLIENT_CAFILE, worker.WORKER_HOSTNAME = str(path / "client-ca.pem"), "localhost"
    worker.SKR_MAA_ENDPOINT = "test.attest.azure.net"
    app = worker.mcp.streamable_http_app()
    app.add_middleware(worker.GatewayAuthMiddleware)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical",
                            timeout_graceful_shutdown=2,
                            **worker.transport_options())
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        url = f"https://localhost:{port}/mcp"
        for name in (None, "wrong-eku", "server", "foreign"):
            context = ssl.create_default_context(cafile=str(path / "server-ca.pem"))
            if name:
                context.load_cert_chain(str(path / f"{name}.pem"), str(path / f"{name}.key"))
            with pytest.raises(httpx.TransportError):
                with httpx.Client(transport=LoopbackTransport(context), trust_env=False, timeout=5) as client:
                    client.get(url)
        context = ssl.create_default_context(cafile=str(path / "server-ca.pem"))
        context.load_cert_chain(str(path / "gateway.pem"), str(path / "gateway.key"))
        async def exercise():
            # Numeric TCP routing must NOT weaken original-hostname TLS verification.
            async with httpx.AsyncClient(
                    transport=AsyncLoopbackTransport(context), trust_env=False, timeout=5) as wrong:
                with pytest.raises(httpx.TransportError):
                    await wrong.get(f"https://wrong-host.invalid:{port}/mcp")
            async with (
                httpx.AsyncClient(transport=AsyncLoopbackTransport(context), timeout=httpx.Timeout(30, read=300),
                                  trust_env=False,
                                  headers={"x-caller-id": "attacker", "x-user-id": "spoof"}) as client,
                streamable_http_client(url, http_client=client) as (read, write, _),
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    async def call(name, args):
                        return await session.call_tool(name, args)
                    assert (await call("write_file", {"session_id": "session-a", "path": "x", "content": "x"})).isError
                    assert not (await call("create_session", {"session_id": "session-a"})).isError
                    assert (await call("create_session", {"session_id": "session-a"})).isError
                    assert not (await call("write_file", {"session_id": "session-a", "path": "x", "content": "x"})).isError
                    assert (await call("read_file", {"session_id": "other", "path": "x"})).isError
                    assert not (await call("delete_session", {"session_id": "session-a"})).isError
                    assert (await call("list_sessions", {})).isError
                    assert (await call("get_attestation", {"nonce": secrets.token_urlsafe(32)})).isError
        asyncio.run(exercise())
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        listener.close()
    assert not thread.is_alive()


def test_generated_code_cannot_see_worker_secrets(monkeypatch, local):
    worker = load_server(monkeypatch, local)
    monkeypatch.setenv("MCP_TLS_KEY", "secret-key")
    monkeypatch.setenv("SOME_TOKEN", "secret-token")
    env = worker._child_env(local)
    assert set(env) == {"PATH", "HOME", "LANG", "PYTHONUNBUFFERED"}
    assert "secret" not in "".join(env.values())


def test_delete_file_and_status(monkeypatch, local):
    worker = load_server(monkeypatch, local)
    worker.create_session("session-a")
    worker.write_file("session-a", "x.txt", "x")
    assert "Deleted" in worker.delete_file("session-a", "x.txt")
    with pytest.raises(ValueError):
        worker.delete_file("session-a", "x.txt")
    with pytest.raises(ValueError):
        worker.delete_file("session-a", "../escape")
    assert worker.get_status() == "active"


def test_worker_never_restarts():
    template = json.loads((ROOT / "infra" / "pool" / "worker.example.json").read_text())
    assert '"restartPolicy": "Never"' in json.dumps(template)


def test_orphan_cleanup_selects_only_old_groups_of_this_pool():
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace as NS
    from orphan_cleanup import expired_groups

    now = datetime(2026, 10, 23, tzinfo=timezone.utc)

    def group(name, pool, age_minutes):
        start = None if age_minutes is None else now - timedelta(minutes=age_minutes)
        container = NS(instance_view=NS(current_state=NS(start_time=start)))
        return NS(name=name, tags={"pool-control": pool}, containers=[container])

    groups = [group("old", "p", 300), group("young", "p", 10),
              group("other-pool", "q", 300), group("not-started", "p", None)]
    result = expired_groups(groups, "p", now, timedelta(minutes=120))
    assert [g.name for g in result] == ["old"]


def test_workspace_size_cap(monkeypatch, local):
    worker = load_server(monkeypatch, local)
    monkeypatch.setattr(worker, "MAX_WORKSPACE_BYTES", 10)
    worker.create_session("session-a")
    worker.write_file("session-a", "small.txt", "x")
    with pytest.raises(ValueError, match="size limit"):
        worker.write_file("session-a", "big.txt", "x" * 100)


def test_non_pooled_mode_rejected(monkeypatch, local):
    monkeypatch.setenv("SANDBOX_WORKSPACE", str(local))
    monkeypatch.setenv("MCP_WORKER_MODE", "legacy")
    spec = importlib.util.spec_from_file_location("bad_config", ROOT / "mcp-build-sandbox" / "server.py")
    with pytest.raises(ValueError, match="Only pooled mode"):
        spec.loader.exec_module(importlib.util.module_from_spec(spec))

def signed_fixture():
    pair = issue("unit-test-signing-only")
    now = int(time.time())
    challenge = canonical_challenge("w", "s", secrets.token_urlsafe(32), "a" * 64)
    import hashlib
    claims = {
        "iss": "https://unit.test", "exp": now + 600, "nbf": now - 1, "iat": now,
        "x-ms-attestation-type": "sevsnpvm", "x-ms-sevsnpvm-hostdata": "b" * 64,
        "x-ms-compliance-status": "azure-compliant-uvm",
        "x-ms-sevsnpvm-reportdata": (hashlib.sha256(challenge).digest() + bytes(32)).hex(),
        "x-ms-sevsnpvm-is-debuggable": False, "x-ms-sevsnpvm-migration-allowed": False,
        "x-ms-sevsnpvm-vmpl": 0, "x-ms-sevsnpvm-smt-allowed": False,
        "x-ms-runtime": json.loads(challenge),
        "x-ms-sevsnpvm-launchmeasurement": "d" * 96,
        "x-ms-sevsnpvm-uvm-endorsement": {
            "x-ms-sevsnpvm-launchmeasurement": "d" * 96, "x-ms-sevsnpvm-guestsvn": "100"},
    }
    minima = dict.fromkeys(("bootloader", "tee", "snpfw", "microcode", "guest"), 1)
    for component in minima:
        claims[f"x-ms-sevsnpvm-{component}{'svn' if component == 'guest' else '-svn'}"] = 1
    minima["uvm_guest"] = 100
    return pair, claims, challenge, minima, now


def test_signed_claim_verifier_is_not_live_attestation():
    pair, claims, challenge, minima, now = signed_fixture()
    token = jwt.encode(claims, pair[0], algorithm="RS256")
    verify_maa_token(token, "https://unit.test", pair[0].public_key(), challenge, "b" * 64, minima, now)
    for changed in ({"x-ms-sevsnpvm-is-debuggable": True}, {"x-ms-sevsnpvm-migration-allowed": True},
                    {"x-ms-sevsnpvm-vmpl": 1}, {"x-ms-sevsnpvm-smt-allowed": True},
                    {"x-ms-sevsnpvm-reportdata": "0" * 128}, {"x-ms-sevsnpvm-hostdata": "c" * 64},
                    {"x-ms-runtime": {}}, {"x-ms-sevsnpvm-tee-svn": 0},
                    {"x-ms-compliance-status": "azure-compliant-cvm"},
                    {"x-ms-sevsnpvm-uvm-endorsement": {}},
                    {"x-ms-sevsnpvm-launchmeasurement": "e" * 96},
                    {"iat": now - 180}, {"iss": "https://other.test"}, {"exp": now - 1}):
        altered = jwt.encode(claims | changed, pair[0], algorithm="RS256")
        with pytest.raises((ValueError, jwt.PyJWTError)):
            verify_maa_token(altered, "https://unit.test", pair[0].public_key(), challenge, "b" * 64, minima, now)
    missing = claims.copy()
    del missing["x-ms-sevsnpvm-reportdata"]
    with pytest.raises(ValueError):
        verify_maa_token(jwt.encode(missing, pair[0], algorithm="RS256"), "https://unit.test",
                         pair[0].public_key(), challenge, "b" * 64, minima, now)
    with pytest.raises(jwt.PyJWTError):
        verify_maa_token(token, "https://unit.test", issue("wrong-key")[0].public_key(),
                         challenge, "b" * 64, minima, now)
    with pytest.raises(ValueError):
        verify_maa_token(token, "https://unit.test", pair[0].public_key(),
                         challenge + b" ", "b" * 64, minima, now)


def test_pki_private_files_refuse_overwrites(local):
    key, cert = pem(issue("test-ca"))
    path = local / "test-ca.key"
    write_private(path, key)
    with pytest.raises(FileExistsError):
        write_private(path, cert)
    assert path.read_text() == key


def test_finalize_is_local_only():
    template = json.loads((ROOT / "infra" / "pool" / "worker.example.json").read_text())
    _, client_ca = pem(issue("test-client-ca"))
    params = {"parameters": {name: {"value": value} for name, value in {
        "workerId": "worker-a", "sessionId": "session-a",
        "workerHostname": "a.example.test", "clientCa": client_ca,
        "maaEndpoint": "maa.example.test",
        "tlsCert": "offline-test-certificate", "tlsKey": "offline-test-key",
    }.items()}}
    assert "identity" not in template["resources"][0]
    assert "imageRegistryCredentials" not in template["resources"][0]["properties"]
    assert not any("registry" in key.lower() or "auth" in key.lower() for key in params["parameters"])
    assert "PRIVATE KEY" not in params["parameters"]["clientCa"]["value"]
    assert "gateway" not in params["parameters"]
    worker = template["resources"][0]["properties"]["containers"][0]
    rules = [{"pattern": env["name"] + "=.*", "strategy": "re2", "required": False}
             for env in worker["properties"]["environmentVariables"]]
    containers = [{"name": "mcp-server", "env_rules": rules},
                  {"name": "attestation-sidecar", "env_rules": []}]
    policy = "package policy\ncontainers := " + json.dumps(containers) + "\nunchanged := true\n"
    template["resources"][0]["properties"]["confidentialComputeProperties"]["ccePolicy"] = base64.b64encode(policy.encode()).decode()
    digest = finalize(template, params)
    final = base64.b64decode(template["resources"][0]["properties"]["confidentialComputeProperties"]["ccePolicy"])
    import hashlib
    assert digest == hashlib.sha256(final).hexdigest()
    assert "unchanged := true" in final.decode()
    definitions = json.loads(final.decode().split("containers := ")[1].splitlines()[0])
    pinned = {r["pattern"].split("=")[0]: r for r in definitions[0]["env_rules"]}
    assert pinned["MCP_TLS_CLIENT_CA"]["strategy"] == "string"
    assert all(r["required"] for r in definitions[0]["env_rules"])
    assert "(?s:MCP_TLS_CERT=.+)" in final.decode()
    assert finalize(template, params) == digest
    platform = {"pattern": "T(E)?MP=.+", "strategy": "re2", "required": False}
    containers[0]["env_rules"] = [*definitions[0]["env_rules"], platform,
                                  {"pattern": "(?i)(FABRIC)_.+=.+", "strategy": "re2", "required": False}]
    broad = base64.b64encode(("package policy\ncontainers := " + json.dumps(containers) + "\n").encode()).decode()
    template["resources"][0]["properties"]["confidentialComputeProperties"]["ccePolicy"] = broad
    finalize(template, params)
    containers[0]["env_rules"].append({"pattern": ".*", "strategy": "re2", "required": False})
    template["resources"][0]["properties"]["confidentialComputeProperties"]["ccePolicy"] = base64.b64encode(
        ("package policy\ncontainers := " + json.dumps(containers) + "\n").encode()).decode()
    with pytest.raises(ValueError, match="broad environment"):
        finalize(template, params)
    params["parameters"]["clientCa"]["value"] += "\nPRIVATE KEY"
    with pytest.raises(ValueError, match="private keys"):
        finalize(template, params)
