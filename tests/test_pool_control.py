import asyncio
import concurrent.futures
import copy
import hashlib
import json
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

import jwt
import pytest
from mcp.types import CallToolResult, TextContent

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pool-control"))
from auth import EntraVerifier
from config import Limits
from controller import Controller
from gateway import Gateway, build_mcp
from store import Conflict, MemoryBackend, PoolStore, TableBackend
from transport import WorkerTransport, PinnedAddressTransport


@pytest.fixture
def state():
    now = [1000.0]
    limits = Limits(idle=60, absolute=240, queue_wait=30, queue_size=30,
                    operation=10, claim=15, upload_bytes=65536, result_bytes=131072)
    return PoolStore(MemoryBackend(), limits, lambda: now[0]), now


class FakeAzure:
    """Explicit fake external boundary, not a production adapter."""
    def __init__(self):
        self.groups, self.deleted, self.uncertain = {}, [], False
        self.crash_create = False

    def inventory(self):
        return {k: v["lifetime"] for k, v in self.groups.items()}

    def healthy(self, worker):
        return worker["id"] in self.groups

    def ensure(self, worker):
        self.groups.setdefault(worker["id"], copy.deepcopy(worker))
        if self.crash_create:
            self.crash_create = False
            raise RuntimeError("accepted create; connection lost")
        return {"hostname": "internal.example", "ip": "10.0.0.2", "spki": "a" * 64,
                "policy_hash": "b" * 64}

    def destroy(self, worker):
        if self.uncertain:
            raise TimeoutError()
        self.groups.pop(worker["id"], None)
        self.deleted.append(worker["id"])
        return True


class FakeTransport:
    def __init__(self):
        self.activated, self.calls, self.established = [], [], []
        self.error, self.uncertain = False, False
        self.pause = None

    async def ready(self, worker):
        pass

    async def activate(self, worker):
        self.activated.append(worker["id"])
        if self.uncertain:
            raise TimeoutError()

    async def establish(self, worker):
        self.established.append(worker["id"])
        return object()

    async def invoke(self, worker, channel, name, arguments):
        return await self.call(worker, name, arguments)

    async def call(self, worker, name, arguments):
        self.calls.append((worker["id"], name, arguments))
        if self.pause:
            self.pause[0].set()
            await self.pause[1].wait()
        if self.uncertain:
            raise TimeoutError()
        return CallToolResult(content=[TextContent(type="text", text="worker output")],
                              isError=self.error, structuredContent={"result": "worker output"})


def active(store, owner="tenant:oid:user"):
    w = store.reserve()
    claim = store.claim(w["id"])
    assert store.complete(w["id"], claim["claim"], "ready", {"hostname": "internal"})
    sid = store.acquire(owner)["session_id"]
    store.assign()
    claim = store.claim(w["id"])
    assert store.complete(w["id"], claim["claim"], "active")
    return sid, w["id"]


def test_configurable_pilot_cap_and_ready_target(monkeypatch):
    for name, value in dict(IDLE=60, ABSOLUTE=240, QUEUE_WAIT=30, QUEUE_SIZE=30, OPERATION=10, CLAIM=15,
                            UPLOAD_BYTES=1, RESULT_BYTES=1, PROVISIONING_AGE=900,
                            TERMINAL_RETENTION=1000).items():
        monkeypatch.setenv("POOL_" + name, str(value))
    assert (Limits.environment().max_total, Limits.environment().desired_ready) == (20, 10)
    monkeypatch.setenv("POOL_MAX_TOTAL", "3")
    monkeypatch.setenv("POOL_DESIRED_READY", "2")
    limits = Limits.environment()
    store = PoolStore(MemoryBackend(), limits, lambda: 1000.0)
    assert [bool(store.reserve()) for _ in range(4)] == [True, True, False, False]
    monkeypatch.setenv("POOL_MAX_TOTAL", "21")
    with pytest.raises(ValueError):
        Limits.environment()
    monkeypatch.setenv("POOL_MAX_TOTAL", "3")
    monkeypatch.setenv("POOL_DESIRED_READY", "4")
    with pytest.raises(ValueError):
        Limits.environment()
    monkeypatch.setenv("POOL_DESIRED_READY", "0")
    assert PoolStore(MemoryBackend(), Limits.environment(), lambda: 1.0).reserve() is None


def test_atomic_concurrent_reservation_assignment_and_dispatch(state):
    store, _ = state
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        workers = [w for w in executor.map(lambda _: store.reserve(), range(40)) if w]
    assert len(workers) == 10
    for w in workers:
        job = store.claim(w["id"])
        store.complete(w["id"], job["claim"], "ready", {"hostname": "internal"})
    sids = [store.acquire("owner")["session_id"] for _ in range(20)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: store.assign(), range(20)))
    rows = store.view()
    assigned = [s["worker"] for k, s in rows.items() if k.startswith("s:") and "worker" in s]
    assert len(assigned) == len(set(assigned)) == 10
    for w in workers:
        job = store.claim(w["id"])
        store.complete(w["id"], job["claim"], "active")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: store.reserve(), range(40)))
    assert sum(k.startswith("w:") for k in store.view()) == 20
    assert store.reserve() is None
    sid = next(s for s in sids if store.status(s, "owner")["status"] == "active")
    def begin():
        try:
            return store.begin(sid, "owner")
        except PermissionError:
            return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        assert sum(v is not None for v in executor.map(lambda _: begin(), range(10))) == 1


def test_owner_expiry_queue_bounds_and_stale_generation(state):
    store, now = state
    sid, wid = active(store)
    with pytest.raises(PermissionError):
        store.begin(sid, "spoof")
    assert store.listing("spoof") == []
    worker, op, gen, _ = store.begin(sid, "tenant:oid:user")
    store.release(sid, "tenant:oid:user")
    assert store.claim(wid) is None
    assert not store.finish(sid, "tenant:oid:user", op, gen)
    now[0] += 11
    delete = store.claim(wid)
    assert delete["state"] == "deleting"
    assert wid in [v["id"] for k, v in store.view().items() if k.startswith("w:")]
    assert not store.complete(wid, "stale-claim", "deleted")
    store.complete(wid, delete["claim"], "deleted")
    assert "w:" + wid not in store.view()
    for _ in range(store.limits.queue_size):
        store.acquire("owner")
    with pytest.raises(ValueError, match="queue full"):
        store.acquire("owner")
    now[0] += 31
    store.sweep()
    assert all(s["status"] == "expired" for s in store.listing("owner"))


def test_activation_crash_is_never_replayed_and_claim_fencing(state):
    store, now = state
    worker = store.reserve()
    job = store.claim(worker["id"])
    now[0] += 16
    newer = store.claim(worker["id"])
    assert not store.complete(worker["id"], job["claim"], "ready", {})
    store.complete(worker["id"], newer["claim"], "ready", {"hostname": "internal"})
    sid = store.acquire("owner")["session_id"]
    store.assign()
    activation = store.claim(worker["id"])
    now[0] += 16
    recovery = store.claim(worker["id"])
    assert recovery["state"] == "deleting"
    assert store.status(sid, "owner")["status"] == "failed"
    assert not store.complete(worker["id"], activation["claim"], "active")


def test_failed_create_call_releases_claim_for_immediate_retry(state):
    store, now = state
    azure, transport = FakeAzure(), FakeTransport()
    original, failures = azure.ensure, [1]
    def flaky(worker):
        if failures[0]:
            failures[0] -= 1
            raise RuntimeError("External boundary failed (EOFError)")
        return original(worker)
    azure.ensure = flaky
    controller = Controller(store, azure, transport)
    async def scenario():
        await controller.tick()  # reserve
        await controller.tick()  # first create attempt dies in the child process
        assert len(azure.groups) == 9
        await controller.tick()  # same clock: the claim was released, no 15 s wait for expiry
        assert len(azure.groups) == 10
    asyncio.run(scenario())


def test_ready_handshake_retries_within_grace_then_recycles(state):
    store, now = state
    azure, transport = FakeAzure(), FakeTransport()
    failures = [2]
    original = transport.ready
    async def flaky(worker):
        if failures[0]:
            failures[0] -= 1
            raise EOFError()
        await original(worker)
    transport.ready = flaky
    controller = Controller(store, azure, transport)
    async def scenario():
        await controller.tick()
        await controller.tick()  # creates; first ready handshake fails but the worker is retained
        states = [v["state"] for k, v in store.view().items() if k.startswith("w:")]
        assert "failed" not in states and "deleting" not in states
        for _ in range(3):
            await controller.tick()
        assert any(v["state"] == "ready" for k, v in store.view().items() if k.startswith("w:"))
        failures[0] = 10 ** 6
        now[0] += store.limits.provisioning_age
        assert store.limits.provisioning_age // 2 < store.limits.provisioning_age
    asyncio.run(scenario())


def test_controller_create_crash_missing_worker_uncertain_delete_and_replenish(state):
    store, now = state
    azure, transport = FakeAzure(), FakeTransport()
    controller = Controller(store, azure, transport)
    async def scenario():
        await controller.tick()  # Reserve before create.
        assert len(store.view()) == 10 and not azure.groups
        await controller.tick()
        assert len(azure.groups) == 10
        sid = store.acquire("owner")["session_id"]
        await controller.tick()
        assert store.status(sid, "owner")["status"] == "active"
        wid = store.view()["s:" + sid]["worker"]
        store.release(sid, "owner")
        azure.uncertain = True
        await controller.tick()
        assert "w:" + wid in store.view()
        azure.uncertain = False
        now[0] += 16
        await controller.tick()
        assert wid in azure.deleted and "w:" + wid not in store.view()
        sid2 = store.acquire("owner")["session_id"]
        await controller.tick()
        wid2 = store.view()["s:" + sid2]["worker"]
        azure.groups.pop(wid2)
        await controller.tick()
        assert store.status(sid2, "owner")["status"] == "failed"
        # ARM succeeded before response loss: capacity is retained until confirmed delete.
        # Consume another ready worker so replenishment creates a reservation.
        store.acquire("owner")
        store.assign()
        pending = store.reserve()
        assert pending is not None
        azure.crash_create = True
        await controller.work(store.claim(pending["id"]))
        assert pending["id"] in azure.groups
        assert store.view()["w:" + pending["id"]]["state"] == "provisioning"
        # Crash/retry observes the same group; it never creates another reservation.
        now[0] += 16
        await controller.work(store.claim(pending["id"]))
        assert store.view()["w:" + pending["id"]]["state"] == "ready"
        store.fail_worker(pending["id"])
        await controller.work(store.claim(pending["id"]))
        assert "w:" + pending["id"] not in store.view()
    asyncio.run(scenario())


@pytest.mark.parametrize("tool,args", [
    ("write_file", {"path": "x", "content": "y"}), ("read_file", {"path": "x"}),
    ("list_dir", {"path": "."}), ("upload_file", {"path": "x", "content_base64": "eA=="}),
    ("download_file", {"path": "x"}), ("run_python", {"code": "print(1)", "timeout": 5}),
    ("run_command", {"command": "echo hi", "timeout": 5}),
    ("pip_install", {"packages": "pytest", "timeout": 5}),
    ("run_tests", {"target": ".", "timeout": 5}),
    ("git_clone", {"repo_url": "https://example.test/repo"}),
    ("get_attestation", {"nonce": "test-nonce"}),
])
def test_all_tools_routed_results_and_errors_unchanged(state, tool, args):
    store, _ = state
    sid, wid = active(store, "owner")
    transport = FakeTransport()
    gateway = Gateway(store, transport, lambda: "owner")
    transport.error = True
    result = asyncio.run(gateway.dispatch(sid, tool, args))
    assert result.isError and result.content[0].text == "worker output"
    call = transport.calls[0]
    assert call[0:2] == (wid, tool)
    if tool != "get_attestation":
        assert call[2]["session_id"] != sid  # Fixed lifetime identity, not client routing.
    assert store.status(sid, "owner")["status"] == "active"


def test_session_key_established_once_and_dropped_on_release(state):
    store, _ = state
    sid, wid = active(store, "owner")
    transport = FakeTransport()
    gateway = Gateway(store, transport, lambda: "owner")
    asyncio.run(gateway.dispatch(sid, "read_file", {"path": "x"}))
    asyncio.run(gateway.dispatch(sid, "read_file", {"path": "y"}))
    assert transport.established == [wid]
    assert sid in gateway.channels
    asyncio.run(gateway.release(sid))
    assert sid not in gateway.channels


def test_gateway_restart_cannot_resume_a_keyed_session(state):
    store, _ = state
    sid, wid = active(store, "owner")
    first = FakeTransport()
    asyncio.run(Gateway(store, first, lambda: "owner").dispatch(sid, "read_file", {"path": "x"}))
    assert first.established == [wid]

    # A restarted gateway has no key, and the worker accepts a session key only once.
    restarted = FakeTransport()
    async def refuse(worker):
        raise PermissionError("already established")
    restarted.establish = refuse
    gateway = Gateway(store, restarted, lambda: "owner")
    with pytest.raises(RuntimeError, match="not replayed"):
        asyncio.run(gateway.dispatch(sid, "read_file", {"path": "x"}))
    assert restarted.calls == []
    with pytest.raises(PermissionError):
        asyncio.run(gateway.dispatch(sid, "read_file", {"path": "x"}))


def test_gateway_uncertain_outcome_no_replay_and_release_race(state):
    store, _ = state
    sid, _ = active(store, "owner")
    transport = FakeTransport()
    gateway = Gateway(store, transport, lambda: "owner")
    transport.uncertain = True
    with pytest.raises(RuntimeError, match="not replayed"):
        asyncio.run(gateway.dispatch(sid, "run_python", {"code": "side_effect()"}))
    with pytest.raises(PermissionError):
        asyncio.run(gateway.dispatch(sid, "run_python", {"code": "side_effect()"}))
    assert len(transport.calls) == 1
    sid, _ = active(store, "owner")
    transport.uncertain = False
    async def race():
        transport.pause = (asyncio.Event(), asyncio.Event())
        task = asyncio.create_task(gateway.dispatch(sid, "read_file", {"path": "x"}))
        await transport.pause[0].wait()
        await gateway.release(sid)
        transport.pause[1].set()
        with pytest.raises(PermissionError, match="discarded"):
            await task
    asyncio.run(race())


def test_bounds_and_no_required_configuration_defaults(state, monkeypatch):
    store, _ = state
    sid, _ = active(store, "owner")
    transport = FakeTransport()
    gateway = Gateway(store, transport, lambda: "owner")
    with pytest.raises(ValueError):
        asyncio.run(gateway.dispatch(sid, "write_file", {"content": "a" * 65537}))
    with pytest.raises(ValueError):
        asyncio.run(gateway.dispatch(sid, "run_python", {"code": "", "timeout": 11}))
    assert not transport.calls
    monkeypatch.delenv("POOL_IDLE", raising=False)
    with pytest.raises(KeyError):
        Limits.environment()


def test_entra_signatures_audience_owner_and_permission():
    from test_pool_feasibility import issue
    key = issue("auth-test-only")[0]
    tenant, oid = str(uuid.uuid4()), str(uuid.uuid4())
    verifier = EntraVerifier(tenant, "pool-api", ["Session.Execute"], [], signing_key=key.public_key())
    now = int(time.time())
    c = {"iss": verifier.issuer, "aud": "pool-api", "tid": tenant, "oid": oid,
         "iat": now, "nbf": now - 1, "exp": now + 60, "roles": ["Session.Execute"]}
    def check(claims, signer=key):
        return asyncio.run(verifier.verify_token(jwt.encode(claims, signer, algorithm="RS256")))
    assert check(c).subject == tenant + ":oid:" + oid
    for changed in ({"roles": []}, {"roles": "Session.Execute"}, {"aud": "other"},
                    {"iss": "https://attacker.test"}, {"exp": now - 1},
                    {"tid": str(uuid.uuid4())}, {"oid": "untrusted"}):
        assert check(c | changed) is None
    assert check(c, issue("wrong-key")[0]) is None
    delegated = EntraVerifier(tenant, "pool-api", [], ["execute"], signing_key=key.public_key())
    assert asyncio.run(delegated.verify_token(jwt.encode(c | {"roles": [], "scp": "execute"},
                                                        key, algorithm="RS256"))).subject.endswith(oid)
    with pytest.raises(ValueError):
        EntraVerifier(tenant, "pool-api", [], [])


def test_table_transaction_adapter_uses_etags_and_one_partition(state):
    from azure.core.exceptions import HttpResponseError
    store, _ = state
    adapter = object.__new__(TableBackend)
    adapter.partition = "pool-test"
    class Client:
        def submit_transaction(self, operations):
            self.operations = operations
    adapter.client = Client()
    adapter.commit("root-etag", 1, {"w:x": ({"state": "ready"}, "worker-etag")},
                   {"w:x": {"state": "active"}, "s:y": {"state": "active"}})
    operations = adapter.client.operations
    assert len(operations) == 3
    assert all(op[1]["PartitionKey"] == "pool-test" for op in operations)
    assert operations[0][2]["etag"] == "root-etag"
    updated = next(op for op in operations if op[1]["RowKey"] == "w:x")
    assert updated[2]["etag"] == "worker-etag"
    def conflict(_):
        error = HttpResponseError()
        error.status_code = 412
        raise error
    adapter.client.submit_transaction = conflict
    with pytest.raises(Conflict):
        adapter.commit("root-etag", 1, {}, {})


def test_real_gateway_entra_mcp_worker_mtls_with_fake_azure_and_maa(
        monkeypatch, state, hostile_loopback_environment):
    """Actual MCP/TLS/auth/activation/routing; local signed evidence is NOT live MAA."""
    import importlib.util
    import secrets
    import shutil
    import ssl
    import httpx
    import uvicorn
    from cryptography.x509.oid import ExtendedKeyUsageOID
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from test_pool_feasibility import issue, pem, signed_fixture
    from pool_worker import public_key_hash
    from conftest import AsyncLoopbackTransport

    store, _ = state
    w = store.reserve()
    hostname = w["id"] + ".pool.test"
    directory = ROOT / ".azure" / "pool-control-tests" / uuid.uuid4().hex
    directory.mkdir(parents=True)
    client_ca, server_ca = issue("client-ca"), issue("server-ca")
    for name, pair in (
        ("client-ca", client_ca), ("server-ca", server_ca),
        ("worker", issue("worker", server_ca, ExtendedKeyUsageOID.SERVER_AUTH, hostname)),
        ("gateway-client", issue("gateway-client", client_ca, ExtendedKeyUsageOID.CLIENT_AUTH)),
        ("gateway-server", issue("gateway-server", server_ca, ExtendedKeyUsageOID.SERVER_AUTH, "localhost")),
    ):
        key, cert = pem(pair)
        (directory / (name + ".key")).write_text(key)
        (directory / (name + ".pem")).write_text(cert)
    monkeypatch.setenv("SANDBOX_WORKSPACE", str(directory / "workspace"))
    monkeypatch.setenv("MCP_WORKER_MODE", "pooled")
    monkeypatch.setenv("MCP_WORKER_ID", w["id"])
    monkeypatch.setenv("MCP_SESSION_ID", w["lifetime"])
    spec = importlib.util.spec_from_file_location("real_pool_worker_" + uuid.uuid4().hex,
                                                 ROOT / "mcp-build-sandbox" / "server.py")
    worker_server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker_server)
    # Avoid package bootstrap in this routing test; real worker shell/files still execute.
    monkeypatch.setattr(worker_server.venv, "create", lambda *args, **kwargs: None)
    worker_server.TLS_CERTFILE = str(directory / "worker.pem")
    worker_server.TLS_KEYFILE = str(directory / "worker.key")
    worker_server.TLS_CLIENT_CAFILE = str(directory / "client-ca.pem")
    worker_server.WORKER_HOSTNAME, worker_server.SKR_MAA_ENDPOINT = hostname, "unit.test"
    # The fake MAA boundary signs the exact worker-generated TLS/identity challenge.
    signing, claims, _, minima, _ = signed_fixture()
    class Response:
        def __init__(self, payload):
            self.text = json.dumps(payload)
        def raise_for_status(self):
            pass
        def json(self):
            return json.loads(self.text)
    def maa(url, json, **kwargs):
        import base64
        challenge = base64.b64decode(json["runtime_data"])
        import json as json_module
        now = int(time.time())
        c = claims | {
            "iss": "https://unit.test", "iat": now, "nbf": now - 1, "exp": now + 60,
            "x-ms-runtime": json_module.loads(challenge),
            "x-ms-sevsnpvm-reportdata": (hashlib.sha256(challenge).digest() + bytes(32)).hex()}
        return Response({"token": jwt.encode(c, signing[0], algorithm="RS256")})
    monkeypatch.setattr(worker_server.httpx, "post", maa)
    original_resolve = socket.getaddrinfo
    failures = [1]
    def resolve(host, port, *args, **kwargs):
        if host in (hostname, hostname.encode()) and failures[0]:
            failures[0] -= 1
            raise socket.gaierror(-2, "Name or service not known")
        return original_resolve("127.0.0.1" if host in (hostname, hostname.encode()) else host,
                                port, *args, **kwargs)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    servers, threads, listeners = [], [], []
    def start(app, tls):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                               log_level="critical", timeout_graceful_shutdown=2, **tls))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
        servers.append(server)
        threads.append(thread)
        listeners.append(listener)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        return port
    try:
        app = worker_server.mcp.streamable_http_app()
        app.add_middleware(worker_server.GatewayAuthMiddleware)
        worker_port = start(app, worker_server.transport_options())
        transport = WorkerTransport(
            str(directory / "server-ca.pem"), str(directory / "gateway-client.pem"),
            str(directory / "gateway-client.key"), "pool.test", "127.0.0.0/8",
            "https://unit.test", minima, store.limits,
            signing_key=signing[0].public_key(), port=worker_port)
        azure = FakeAzure()
        routing = {"hostname": hostname, "ip": "127.0.0.1",
                   "spki": public_key_hash((directory / "worker.pem").read_bytes()),
                   "policy_hash": "b" * 64}
        def ensure(candidate):
            if candidate["id"] != w["id"]:
                return None
            azure.groups[w["id"]] = candidate
            return routing
        azure.ensure = ensure
        controller = Controller(store, azure, transport)
        asyncio.run(controller.tick())
        assert store.view()["w:" + w["id"]]["state"] == "ready"
        tenant, oid = str(uuid.uuid4()), str(uuid.uuid4())
        verifier = EntraVerifier(tenant, "pool-api", ["Execute"], [], signing_key=signing[0].public_key())
        # Reserve listener first so the configured canonical ingress host includes its port.
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        gateway_port = listener.getsockname()[1]
        resource = f"https://localhost:{gateway_port}/mcp"
        gateway_mcp = build_mcp(Gateway(store, transport), verifier, resource)
        gateway_server = uvicorn.Server(uvicorn.Config(
            gateway_mcp.streamable_http_app(), host="127.0.0.1", port=gateway_port,
            log_level="critical", timeout_graceful_shutdown=2,
            ssl_certfile=str(directory / "gateway-server.pem"),
            ssl_keyfile=str(directory / "gateway-server.key")))
        thread = threading.Thread(target=gateway_server.run, kwargs={"sockets": [listener]})
        servers.append(gateway_server)
        threads.append(thread)
        listeners.append(listener)
        thread.start()
        deadline = time.monotonic() + 10
        while not gateway_server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert gateway_server.started
        now = int(time.time())
        token_claims = {"iss": verifier.issuer, "aud": "pool-api", "tid": tenant, "oid": oid,
                        "iat": now, "nbf": now - 1, "exp": now + 120, "roles": ["Execute"]}
        token = jwt.encode(token_claims, signing[0], algorithm="RS256")
        async def scenario():
            context = ssl.create_default_context(cafile=str(directory / "server-ca.pem"))
            async with httpx.AsyncClient(transport=AsyncLoopbackTransport(context), trust_env=False) as unauth:
                assert (await unauth.post(resource)).status_code == 401
            async with (
                httpx.AsyncClient(transport=AsyncLoopbackTransport(context), trust_env=False,
                                  headers={"Authorization": "Bearer " + token,
                                           "X-User-Id": "attacker"}, timeout=20) as client,
                streamable_http_client(resource, http_client=client) as (read, write, _),
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert {t.name for t in tools.tools} == {
                        "create_session", "get_session_status", "list_sessions", "delete_session",
                        "write_file", "read_file", "list_dir", "upload_file", "download_file",
                        "run_python", "run_command", "pip_install", "run_tests", "git_clone", "get_attestation"}
                    created = await session.call_tool("create_session", {})
                    sid = json.loads(created.content[0].text)["session_id"]
                    assert store.view()["s:" + sid]["owner"] == tenant + ":oid:" + oid
                    await controller.tick()
                    assert store.status(sid, tenant + ":oid:" + oid)["status"] == "active"
                    for name, args in (
                        ("write_file", {"path": "data.txt", "content": "real routed content"}),
                        ("read_file", {"path": "data.txt"}),
                        ("run_command", {"command": "echo pool-integration", "timeout": 5}),
                        ("get_attestation", {"nonce": secrets.token_urlsafe(32)}),
                    ):
                        result = await session.call_tool(name, {"session_id": sid, **args})
                        assert not result.isError, result
                        if name == "read_file":
                            assert result.content[0].text == "real routed content"
                    error = await session.call_tool("read_file", {"session_id": sid, "path": "missing"})
                    assert error.isError  # Worker tool errors retained, no gateway replay.
                    assert not (await session.call_tool("delete_session", {"session_id": sid})).isError
                    assert (await session.call_tool("read_file", {"session_id": sid, "path": "data.txt"})).isError
                    await controller.tick()
                    assert "w:" + w["id"] not in store.view() and w["id"] in azure.deleted
        asyncio.run(scenario())
    finally:
        for server in servers:
            server.should_exit = True
        for thread in threads:
            thread.join(timeout=15)
        for listener in listeners:
            listener.close()
        shutil.rmtree(directory)
    assert not any(thread.is_alive() for thread in threads)


def test_live_adapter_material_sdk_mapping_and_strict_resource_dns_bounds():
    """Exercise actual SDK models without contacting ARM or a credential service."""
    import base64
    from types import SimpleNamespace
    from azure.core.exceptions import ResourceNotFoundError
    from azure.mgmt.containerinstance.models import ContainerGroup
    from azure_adapter import AzurePool
    from test_pool_feasibility import issue, pem
    from cryptography.x509.oid import ExtendedKeyUsageOID

    adapter = object.__new__(AzurePool)
    adapter.rg, adapter.pool, adapter.prefix, adapter.location = "dedicated-rg", "pilot", "pilot-worker", "eastus2"
    adapter.subnet_id = "/subscriptions/test/resourceGroups/network/providers/Microsoft.Network/virtualNetworks/v/subnets/pool"
    adapter.dns_group, adapter.zone = "network-rg", "pool.test"
    import ipaddress
    adapter.network = ipaddress.ip_network("10.0.0.0/24")
    worker = {"id": uuid.uuid4().hex, "lifetime": uuid.uuid4().hex,
              "claim": uuid.uuid4().hex, "claim_until": time.time() + 60,
              "create_operation": uuid.uuid4().hex, "delete_operation": uuid.uuid4().hex}
    host = worker["id"] + ".pool.test"
    server_ca, client_ca = issue("server-ca"), issue("client-ca")
    key, cert = pem(issue("worker", server_ca, ExtendedKeyUsageOID.SERVER_AUTH, host))
    policy = b"TEST_FIXTURE_ONLY_NOT_A_CCE_POLICY"
    values = {
        "MCP_WORKER_MODE": "pooled", "MCP_WORKER_ID": worker["id"],
        "MCP_SESSION_ID": worker["lifetime"], "MCP_WORKER_HOSTNAME": host,
        "MCP_TLS_CERT": cert, "MCP_TLS_KEY": key,
        "MCP_TLS_CLIENT_CA": pem(client_ca)[1], "SKR_MAA_ENDPOINT": "unit.test"}
    containers = [
        {"name": "mcp-server", "properties": {
            "image": "private.example/worker@sha256:" + "a" * 64,
            "environmentVariables": [{"name": k, "secureValue" if k in ("MCP_TLS_KEY", "MCP_TLS_CERT") else "value": v}
                                     for k, v in values.items()],
            "resources": {"requests": {"cpu": 2, "memoryInGB": 4}}}},
        {"name": "attestation-sidecar", "properties": {
            "image": "private.example/sidecar@sha256:" + "b" * 64,
            "resources": {"requests": {"cpu": 1, "memoryInGB": 1}}}}]
    material = {"server_cert": cert, "policy_hash": hashlib.sha256(policy).hexdigest(),
                "group": {"name": adapter.name(worker), "location": "eastus2",
                          "properties": {"sku": "Confidential", "osType": "Linux",
                                         "ipAddress": {"type": "Private"},
                                         "subnetIds": [{"id": adapter.subnet_id}],
                                         "confidentialComputeProperties": {
                                             "ccePolicy": base64.b64encode(policy).decode()},
                                         "containers": containers}}}
    adapter.validate_material(worker, host, material)
    model = ContainerGroup.from_dict(material["group"])
    assert model.sku == "Confidential"
    assert model.confidential_compute_properties.cce_policy
    assert model.containers[0].environment_variables[5].secure_value == key
    for change in ("identity", "imageRegistryCredentials", "subnetIds"):
        altered = copy.deepcopy(material)
        if change == "identity":
            altered["group"]["identity"] = {"type": "SystemAssigned"}
        else:
            altered["group"]["properties"][change] = [{"id": "unapproved"}]
        with pytest.raises(ValueError):
            adapter.validate_material(worker, host, altered)
    tagged = SimpleNamespace(name=adapter.name(worker), tags=adapter.tags(worker))
    assert adapter.owned(tagged, worker) is tagged
    with pytest.raises(PermissionError):
        adapter.owned(SimpleNamespace(name="legacy-demo", tags=adapter.tags(worker)), worker)
    with pytest.raises(PermissionError):
        adapter.owned(SimpleNamespace(name=adapter.name(worker), tags={"pool-control": "pilot"}), worker)
    class DNS:
        def get(self, *args):
            self.args = args
            return SimpleNamespace(metadata={"unrelated": "true"}, etag="etag")
        def create_or_update(self, *args, **kwargs):
            raise AssertionError("Must not alter unrelated DNS")
    adapter.dns = DNS()
    with pytest.raises(PermissionError):
        adapter.put_dns(worker, "10.0.0.2")
    assert adapter.dns.args == ("network-rg", "pool.test", "A", worker["id"])
    # Strict tag checks also protect deletion, before any management write.
    adapter.groups = SimpleNamespace(get=lambda *args: SimpleNamespace(name="legacy-demo", tags={}))
    with pytest.raises(PermissionError):
        adapter.destroy(worker)


def test_idle_absolute_expiry_and_inflight_crash_retire(state):
    store, now = state
    sid, _ = active(store, "owner")
    now[0] += 61
    assert store.status(sid, "owner")["status"] == "expired"
    sid, wid = active(store, "owner")
    store.begin(sid, "owner")  # Simulate gateway process death after dispatch.
    now[0] += 11
    store.sweep()
    assert store.status(sid, "owner")["status"] == "failed"
    assert store.claim(wid)["state"] == "deleting"
    sid, _ = active(store, "owner")
    now[0] += 239
    # Keep activity but never extend the absolute deadline.
    store.change(lambda rows, now: rows["s:" + sid].update(idle_until=now + 60))
    _, op, generation, remaining = store.begin(sid, "owner")
    assert remaining == 1
    now[0] += 2
    assert not store.finish(sid, "owner", op, generation)
    assert store.status(sid, "owner")["status"] == "failed"


def test_pinned_worker_transport_refuses_unverified_tls_and_changed_origins():
    import ssl
    context = ssl.create_default_context()
    transport = PinnedAddressTransport(context, "worker.pool.test", "10.0.0.2", 8000)
    import httpx
    async def check():
        async with transport:
            for url in ("https://attacker.test:8000/mcp", "http://worker.pool.test:8000/mcp",
                        "https://worker.pool.test:9000/mcp"):
                with pytest.raises(ValueError, match="origin"):
                    await transport.handle_async_request(httpx.Request("POST", url))
    asyncio.run(check())
    context.check_hostname = False
    with pytest.raises(ValueError, match="verification"):
        PinnedAddressTransport(context, "worker.pool.test", "10.0.0.2", 8000)


@pytest.fixture
def material_directory():
    import shutil
    directory = ROOT / ".azure" / "pool-material-tests" / uuid.uuid4().hex
    directory.mkdir(parents=True)
    try:
        yield directory
    finally:
        shutil.rmtree(directory)


def test_durable_material_real_cert_finalizer_idempotency_and_retirement(material_directory):
    """Fake confcom runner is test-only; real cert issuance and finalizer are exercised."""
    import base64
    from material import MaterialProvider, pull_configuration
    from test_pool_feasibility import issue, pem
    from pool_worker import public_key_hash
    from cryptography import x509
    from cryptography.x509.oid import ExtendedKeyUsageOID

    directory = material_directory
    for name in ("server-ca", "client-ca"):
        key, cert = pem(issue(name))
        (directory / (name + ".pem")).write_text(cert)
        (directory / (name + ".key")).write_text(key)
    calls = []
    def generate(template_path, params_path):
        calls.append(template_path)
        assert set(json.loads(params_path.read_text())["parameters"]) .isdisjoint({"tlsCert", "tlsKey"})
        template = json.loads(template_path.read_text())
        worker = template["resources"][0]["properties"]["containers"][0]
        definitions = [{"name": "mcp-server", "env_rules": [
            {"pattern": e["name"] + "=.*", "strategy": "re2", "required": False}
            for e in worker["properties"]["environmentVariables"]]},
            {"name": "attestation-sidecar", "env_rules": []}]
        fixture = "package policy\ncontainers := " + json.dumps(definitions) + "\n"
        template["resources"][0]["properties"]["confidentialComputeProperties"]["ccePolicy"] = base64.b64encode(fixture.encode()).decode()
        template_path.write_text(json.dumps(template))
    identity = "/subscriptions/" + str(uuid.uuid4()) + "/resourceGroups/pull/providers/Microsoft.ManagedIdentity/userAssignedIdentities/pull-only"
    pull = pull_configuration(identity, "registry.azurecr.io", "true", "acr-pull-only")
    def provider():
        return MaterialProvider(directory / "material", directory / "server-ca.pem", directory / "server-ca.key",
                                directory / "client-ca.pem", "registry.azurecr.io/worker@sha256:" + "a" * 64,
                                "registry.azurecr.io/sidecar@sha256:" + "b" * 64, "pilot", "eastus2",
                                "/subscriptions/test/subnet", "https://unit.test", 3, 10, "az.cmd",
                                pull=pull, runner=generate)
    worker = {"id": uuid.uuid4().hex, "lifetime": uuid.uuid4().hex, "created": time.time()}
    hostname = worker["id"] + ".pool.test"
    first = provider().prepare(worker, hostname)
    second = provider().prepare(worker, hostname)
    assert first == second and len(calls) == 1
    cert = x509.load_pem_x509_certificate(first["server_cert"].encode())
    assert cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value == x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH])
    assert first["group"]["identity"]["userAssignedIdentities"] == {identity: {}}
    assert first["group"]["properties"]["imageRegistryCredentials"] == [{"identity": identity, "server": "registry.azurecr.io"}]
    policy = base64.b64decode(first["group"]["properties"]["confidentialComputeProperties"]["ccePolicy"])
    assert hashlib.sha256(policy).hexdigest() == first["policy_hash"]
    assert worker["id"].encode() in policy and b"MCP_TLS_CLIENT_CA=" in policy
    other = {"id": uuid.uuid4().hex, "lifetime": uuid.uuid4().hex, "created": time.time()}
    other_material = provider().prepare(other, other["id"] + ".pool.test")
    assert public_key_hash(first["server_cert"].encode()) != public_key_hash(other_material["server_cert"].encode())
    with pytest.raises(PermissionError):
        provider().prepare(worker | {"lifetime": uuid.uuid4().hex}, hostname)
    # A failed generation retains its issued key so retry cannot silently rotate identity.
    failed = {"id": uuid.uuid4().hex, "lifetime": uuid.uuid4().hex, "created": time.time()}
    failing = provider()
    failing.runner = lambda *args: (_ for _ in ()).throw(TimeoutError())
    with pytest.raises(TimeoutError):
        failing.prepare(failed, failed["id"] + ".pool.test")
    issued = json.loads((directory / "material" / failed["id"] / "tls.json").read_text())["cert"]
    assert provider().prepare(failed, failed["id"] + ".pool.test")["server_cert"] == issued
    provider().forget(worker)
    provider().forget(worker)
    assert not (directory / "material" / worker["id"]).exists()
    with pytest.raises(PermissionError):
        provider().prepare(worker, hostname)
    with pytest.raises(ValueError):
        pull_configuration(identity, "registry.azurecr.io", "false", "acr-pull-only")


def test_base_policy_injection_binds_images_and_shape(material_directory):
    import base64
    import shutil
    from material import MaterialProvider, pull_configuration, template_shape, worker_template
    from test_pool_feasibility import issue, pem
    directory = material_directory
    for name in ("server-ca", "client-ca"):
        key, cert = pem(issue(name))
        (directory / (name + ".pem")).write_text(cert)
        (directory / (name + ".key")).write_text(key)
    identity = "/subscriptions/" + str(uuid.uuid4()) + "/resourceGroups/pull/providers/Microsoft.ManagedIdentity/userAssignedIdentities/pull-only"
    pull = pull_configuration(identity, "registry.azurecr.io", "true", "acr-pull-only")
    images = ("registry.azurecr.io/worker@sha256:" + "a" * 64, "registry.azurecr.io/sidecar@sha256:" + "b" * 64)
    definitions = [{"name": "mcp-server", "env_rules": [
        {"pattern": e["name"] + "=.*", "strategy": "re2", "required": False}
        for e in worker_template(images, pull)["resources"][0]["properties"]["containers"][0]["properties"]["environmentVariables"]]},
        {"name": "attestation-sidecar", "env_rules": []}]
    policy = base64.b64encode(("package policy\ncontainers := " + json.dumps(definitions) + "\n").encode()).decode()
    base = directory / "base.json"
    shape = template_shape(worker_template(images, pull))
    base.write_text(json.dumps({"images": list(images), "shape": shape, "ccePolicy": policy}))
    def provider(path=base, image=images[0]):
        return MaterialProvider(directory / "material", directory / "server-ca.pem", directory / "server-ca.key",
                                directory / "client-ca.pem", image, images[1], "pilot", "eastus2",
                                "/subscriptions/test/subnet", "https://unit.test", 3, 10, "az.cmd",
                                pull=pull, policy_base=str(path))
    worker = {"id": uuid.uuid4().hex, "lifetime": uuid.uuid4().hex, "created": time.time()}
    result = provider().prepare(worker, worker["id"] + ".pool.test")
    final = base64.b64decode(result["group"]["properties"]["confidentialComputeProperties"]["ccePolicy"])
    assert hashlib.sha256(final).hexdigest() == result["policy_hash"]
    assert ("MCP_WORKER_ID=" + worker["id"]).encode() in final
    shutil.rmtree(directory / "material")  # simulated controller restart: nothing persisted
    again = provider().prepare(worker, worker["id"] + ".pool.test")
    assert again["server_cert"] == result["server_cert"] and again["policy_hash"] == result["policy_hash"]
    with pytest.raises(ValueError, match="Base policy"):
        provider(image="registry.azurecr.io/worker@sha256:" + "c" * 64)
    base.write_text(json.dumps({"images": list(images), "shape": "0" * 64, "ccePolicy": policy}))
    with pytest.raises(ValueError, match="Base policy"):
        provider()


def test_provisioning_quarantine_admin_recovery_and_terminal_gc(state):
    store, now = state
    worker = store.reserve()
    job = store.claim(worker["id"])
    now[0] += store.limits.provisioning_age + 1
    store.sweep()
    assert store.view()["w:" + worker["id"]]["state"] == "quarantined"
    assert store.claim(worker["id"]) is None
    assert not store.complete(worker["id"], job["claim"], "ready", {})
    with pytest.raises(PermissionError):
        store.recover(worker["id"], "retry-provisioning", "wrong-lifetime")
    store.recover(worker["id"], "retry-provisioning", worker["lifetime"])
    assert store.claim(worker["id"])["state"] == "provisioning"
    with pytest.raises(PermissionError):
        store.recover(worker["id"], "destroy", worker["lifetime"])
    now[0] += store.limits.claim + 1
    store.recover(worker["id"], "destroy", worker["lifetime"])
    assert "w:" + worker["id"] in store.view()
    delete = store.claim(worker["id"])
    store.complete(worker["id"], delete["claim"], "deleted")
    sid, wid = active(store, "owner")
    store.release(sid, "owner")
    now[0] += store.limits.terminal_retention + 1
    store.sweep()
    assert "s:" + sid in store.view()  # Retain until corresponding worker is gone.
    delete = store.claim(wid)
    store.complete(wid, delete["claim"], "deleted")
    store.sweep()
    assert "s:" + sid not in store.view()


def test_overcap_owned_orphans_pause_admission_and_drain_without_freeing_slots(state):
    store, _ = state
    for _ in range(22):
        store.reconcile_orphan(uuid.uuid4().hex, uuid.uuid4().hex)
    rows = store.view()
    assert sum(k.startswith("w:") for k in rows) == 22
    assert rows["control"]["admission_paused"]
    assert store.reserve() is None
    with pytest.raises(PermissionError):
        store.acquire("owner")
    with pytest.raises(ValueError):
        store.recover(None, "resume-admission", None)
    workers = [v for k, v in rows.items() if k.startswith("w:")]
    for worker in workers[:2]:
        job = store.claim(worker["id"])
        store.complete(worker["id"], job["claim"], "deleted")
    assert store.view()["control"]["admission_paused"]
    store.recover(None, "resume-admission", None)
    assert store.acquire("owner")["status"] == "queued"
    assert store.reserve() is None  # Still at twenty pending-confirmed-delete rows.


class ProcessBoundary:
    def snapshot(self):
        return ("etag", 1, {})

    def slow(self):
        time.sleep(60)

    def conflict(self):
        raise Conflict()


def process_boundary_factory():
    return ProcessBoundary()


def test_spawned_sdk_boundary_hard_deadline_and_conflict_propagation():
    from bounded import invoke
    assert invoke(process_boundary_factory, "snapshot", (), {}, 15) == ("etag", 1, {})
    with pytest.raises(Conflict):
        invoke(process_boundary_factory, "conflict", (), {}, 15)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        invoke(process_boundary_factory, "slow", (), {}, 3)
    assert time.monotonic() - started < 9
