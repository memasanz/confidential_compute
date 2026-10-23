"""Offline examples: explicit fake service/model boundaries, never Azure requests."""

import ast
import asyncio
import inspect
import json
import shutil
import socket
import ssl
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2
import nbformat
import pytest
from mcp.types import CallToolResult, TextContent
from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))

import agent_framework_analysis as agent_example
import pool_client
import pooled_analysis
from pool_client import PoolClient, PoolConfig


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("Example tests must not access the network")
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(pool_client, "load_dotenv", lambda *args, **kwargs: None)


def config(tmp_path):
    return PoolConfig("https://gateway.example/mcp", tmp_path / "gateway.pem",
                      "11111111-1111-4111-8111-111111111111")


def test_config_paths_scope_and_secret_redaction(monkeypatch, tmp_path):
    pem = tmp_path / "gateway.pem"
    pem.write_text("test certificate placeholder")
    monkeypatch.setattr(pool_client, "EXAMPLES", tmp_path)
    settings = {
        "POOL_GATEWAY_URL": "https://gateway.example/mcp",
        "POOL_GATEWAY_CA": "gateway.pem",
        "MCP_API_APP_ID": "11111111-1111-4111-8111-111111111111",
        "MCP_TENANT_ID": "tenant", "MCP_CLIENT_ID": "client",
        "MCP_CLIENT_SECRET": "local-test-value",
    }
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    loaded = PoolConfig.from_env()
    assert loaded.gateway_ca == pem
    assert loaded.scope == f"api://{settings['MCP_API_APP_ID']}/.default"
    assert "local-test-value" not in repr(loaded)
    monkeypatch.setenv("POOL_GATEWAY_URL", "http://gateway.example/mcp")
    with pytest.raises(ValueError, match="HTTPS"):
        PoolConfig.from_env()
    monkeypatch.delenv("MCP_API_APP_ID")
    monkeypatch.setenv("POOL_GATEWAY_URL", "https://gateway.example/mcp")
    with pytest.raises(ValueError, match="MCP_API_APP_ID"):
        PoolConfig.from_env()


def test_tls_preserves_verification(monkeypatch, tmp_path):
    context = ssl.create_default_context()
    factory = lambda **kwargs: context
    monkeypatch.setattr(pool_client.ssl, "create_default_context", factory)
    result = config(tmp_path).tls_context()
    assert result.check_hostname and result.verify_mode == ssl.CERT_REQUIRED
    assert result.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN


def test_mcp_bearer_scope_and_result_errors(monkeypatch, tmp_path):
    http_options = []
    credential = SimpleNamespace(get_token=AsyncMock(return_value=SimpleNamespace(token="offline-token")))
    result = CallToolResult(content=[TextContent(type="text", text="ok")])
    session = SimpleNamespace(
        initialize=AsyncMock(), call_tool=AsyncMock(side_effect=lambda *args: result))

    @asynccontextmanager
    async def http(**kwargs):
        http_options.append(kwargs)
        yield object()

    @asynccontextmanager
    async def transport(*args, **kwargs):
        yield None, None, None

    @asynccontextmanager
    async def connect(*args):
        yield session

    monkeypatch.setattr(pool_client.httpx, "AsyncClient", http)
    monkeypatch.setattr(pool_client, "streamable_http_client", transport)
    monkeypatch.setattr(pool_client, "ClientSession", connect)

    async def run():
        client = PoolClient(config(tmp_path))
        client.credential, client.tls = credential, ssl.create_default_context()
        assert await client.call("list_dir", session_id="bound") == "ok"
        credential.get_token.assert_awaited_with(client.config.scope)
        assert http_options[0]["headers"]["Authorization"] == "Bearer offline-token"
        assert http_options[0]["trust_env"] is False
        assert http_options[0]["verify"].check_hostname
        result.isError = True
        with pytest.raises(RuntimeError, match="list_dir failed"):
            await client.call("list_dir", session_id="bound")
        assert session.call_tool.await_count == 2  # No automatic tool replay.

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["body", "expired", "cancelled", "cleanup", "both", None])
def test_session_cleanup_on_all_paths(tmp_path, failure):
    async def run():
        client = PoolClient(config(tmp_path))
        calls = []

        async def call(name, **kwargs):
            calls.append(name)
            if name == "create_session":
                return '{"session_id":"bound"}'
            if name == "get_session_status":
                return json.dumps({"status": "expired" if failure == "expired" else "active"})
            if name == "delete_session" and failure in ("cleanup", "both"):
                raise RuntimeError("cleanup failed")
            return "released"

        client.call = call

        async def operation():
            async with client.session() as sid:
                assert sid == "bound"
                if failure in ("body", "both"):
                    raise ValueError("body failed")
                if failure == "cancelled":
                    raise asyncio.CancelledError()

        if failure == "both":
            with pytest.raises(ExceptionGroup) as errors:
                await operation()
            assert [str(e) for e in errors.value.exceptions] == ["body failed", "cleanup failed"]
        elif failure == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await operation()
        elif failure:
            with pytest.raises((ValueError, RuntimeError)):
                await operation()
        else:
            await operation()
        assert calls[-1] == "delete_session"
        assert calls.count("delete_session") == 1

    asyncio.run(run())


def test_readiness_timeout_is_bounded(tmp_path):
    async def run():
        client = PoolClient(config(tmp_path))
        client.call = AsyncMock(return_value='{"status":"queued"}')
        with pytest.raises(TimeoutError):
            await client.wait_until_active("bound", timeout=0.01, poll_interval=0.001)
    asyncio.run(run())


def test_readiness_accepts_worker_activation(tmp_path):
    async def run():
        client = PoolClient(config(tmp_path))
        client.call = AsyncMock(side_effect=[
            '{"status":"queued"}', '{"status":"activating"}', '{"status":"active"}',
        ])
        await client.wait_until_active("bound", timeout=1, poll_interval=0.001)
        assert client.call.await_count == 3
    asyncio.run(run())


class FakePool:
    """Local simulation for tests only: no attestation or confidential execution."""

    def __init__(self, config, workspace):
        self.workspace = workspace
        self.calls = []
        self.released = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    @asynccontextmanager
    async def session(self):
        try:
            yield "not-model-visible"
        finally:
            self.released = True

    async def list_tools(self):
        return ["run_python", "list_dir", "upload_file", "download_file"]

    async def upload(self, sid, source, destination="input.xlsx"):
        shutil.copyfile(source, self.workspace / destination)

    async def download(self, sid, source, destination):
        shutil.copyfile(self.workspace / source, destination)
        return Path(destination)

    async def call(self, name, **arguments):
        self.calls.append((name, arguments))
        assert arguments["session_id"] == "not-model-visible"
        if name == "run_python":
            result = subprocess.run(
                [sys.executable, "-c", arguments["code"]], cwd=self.workspace,
                capture_output=True, text=True, timeout=30, check=True,
            )
            return result.stdout
        if name == "get_session_status":
            return '{"status":"active"}'
        if name == "write_file":
            (self.workspace / arguments["path"]).write_text(arguments["content"])
            return "written"
        if name == "read_file":
            return (self.workspace / arguments["path"]).read_text()
        if name == "list_dir":
            return json.dumps([p.name for p in self.workspace.iterdir()])
        raise AssertionError(f"Unexpected fake tool: {name}")


class FakeCredential:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass


class FakeProject:
    def __init__(self, **kwargs):
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.openai.close()

    def get_openai_client(self, **kwargs):
        def respond(request):
            body = json.loads(request.content)
            self.requests.append(body)
            assert body["model"] == "offline-model"
            assert "not-model-visible" not in json.dumps(body)
            for tool in body.get("tools", []):
                properties = tool["parameters"]["properties"]
                assert set(properties) <= {"code"}
            if len(self.requests) == 1:
                output = [{
                    "id": "offline-function", "type": "function_call",
                    "call_id": "offline-call", "name": "run_python",
                    "arguments": json.dumps({"code": pooled_analysis.ANALYSIS}),
                    "status": "completed",
                }]
            else:
                output = [{
                    "id": "offline-message", "type": "message", "role": "assistant",
                    "status": "completed", "content": [{
                        "type": "output_text", "annotations": [],
                        "text": "Synthetic revenue sum is 640; units sum is 64.",
                    }],
                }]
            return httpx2.Response(200, json={
                "id": "offline-response", "object": "response", "created_at": 0,
                "model": "offline-model", "status": "completed", "output": output,
                "parallel_tool_calls": False, "tool_choice": "auto", "tools": body.get("tools", []),
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            })

        self.openai = AsyncOpenAI(
            api_key="offline-placeholder", base_url="https://model.example/v1",
            max_retries=0, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)),
        )
        return self.openai


def example_boundaries(monkeypatch, tmp_path):
    workspace = tmp_path / "worker"
    workspace.mkdir()
    output = tmp_path / "examples"
    output.mkdir()
    shutil.copyfile(ROOT / "examples" / "sample_sales.xlsx", output / "sample_sales.xlsx")
    pool = FakePool(None, workspace)
    monkeypatch.setattr(pool_client, "EXAMPLES", output)
    monkeypatch.setattr(agent_example, "EXAMPLES", output)
    monkeypatch.setattr(pooled_analysis, "EXAMPLES", output)
    monkeypatch.setattr(PoolConfig, "from_env", classmethod(lambda cls: config(tmp_path)))
    monkeypatch.setattr(pool_client, "PoolClient", lambda *args: pool)
    monkeypatch.setattr(agent_example, "PoolClient", lambda *args: pool)
    monkeypatch.setattr(pooled_analysis, "PoolClient", lambda *args: pool)
    monkeypatch.setattr(agent_example, "DefaultAzureCredential", FakeCredential)
    monkeypatch.setattr(agent_example, "AIProjectClient", FakeProject)
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://model.example/api/projects/offline")
    monkeypatch.setenv("FOUNDRY_MODEL", "offline-model")
    monkeypatch.delenv("MCP_EXCEL_PATH", raising=False)
    return pool, output


def test_runnable_agent_with_real_framework_fake_model(monkeypatch, tmp_path):
    pool, output = example_boundaries(monkeypatch, tmp_path)
    asyncio.run(agent_example.main())
    assert pool.released
    assert agent_example.validate_summary(output / "agent_summary.csv")
    assert [name for name, _ in pool.calls] == ["run_python"]


def test_runnable_agent_releases_after_model_failure(monkeypatch, tmp_path):
    pool, _ = example_boundaries(monkeypatch, tmp_path)
    monkeypatch.setattr(agent_example, "analyze", AsyncMock(side_effect=RuntimeError("model failed")))
    with pytest.raises(RuntimeError, match="model failed"):
        asyncio.run(agent_example.main())
    assert pool.released


@pytest.mark.parametrize("name", ["01_mcp_tools.ipynb", "02_agent_framework.ipynb"])
def test_notebook_schema_and_execution_offline(monkeypatch, tmp_path, name):
    pool, output = example_boundaries(monkeypatch, tmp_path)
    import azure.ai.projects.aio
    import azure.identity.aio
    monkeypatch.setattr(azure.ai.projects.aio, "AIProjectClient", FakeProject)
    monkeypatch.setattr(azure.identity.aio, "DefaultAzureCredential", FakeCredential)
    monkeypatch.chdir(ROOT)
    notebook = nbformat.read(ROOT / "examples" / name, as_version=4)
    nbformat.validate(notebook)
    namespace = {"__name__": "__notebook__"}

    async def run():
        for cell in notebook.cells:
            if cell.cell_type != "code":
                continue
            assert cell.execution_count is None and cell.outputs == []
            code = compile(cell.source, name, "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
            result = eval(code, namespace)
            if inspect.isawaitable(result):
                await result

    asyncio.run(run())
    assert pool.released
    filename = "summary.csv" if name.startswith("01") else "agent_summary.csv"
    agent_example.validate_summary(output / filename)


def test_invalid_artifact_is_not_success(tmp_path):
    artifact = tmp_path / "summary.csv"
    artifact.write_text("metric,revenue,units\ncount,4,4\nmean,160,16\nsum,999,64\n")
    with pytest.raises(ValueError, match="Incorrect"):
        agent_example.validate_summary(artifact)
