"""Small MCP client shared by the scripts and notebooks."""

import asyncio
import base64
import json
import os
import ssl
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from azure.identity.aio import ClientSecretCredential, DefaultAzureCredential
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent

EXAMPLES = Path(__file__).resolve().parent
QUEUE_WAIT_SECONDS = 15 * 60


def required_setting(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Set {name} in the environment or examples\\.env")
    return value


def example_path(value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else EXAMPLES / path


@dataclass(frozen=True)
class PoolConfig:
    gateway_url: str
    gateway_ca: Path
    api_app_id: str
    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = field(default="", repr=False)

    @classmethod
    def from_env(cls):
        load_dotenv(EXAMPLES / ".env", override=False)
        url = required_setting("POOL_GATEWAY_URL")
        parts = urlsplit(url)
        if (parts.scheme != "https" or not parts.hostname or parts.username
                or parts.password or parts.query or parts.fragment):
            raise ValueError("POOL_GATEWAY_URL must be an HTTPS MCP endpoint without credentials")
        ca = example_path(required_setting("POOL_GATEWAY_CA"))
        if not ca.is_file():
            raise FileNotFoundError("POOL_GATEWAY_CA must point to the verified gateway PEM")
        app_id = str(UUID(required_setting("MCP_API_APP_ID")))
        secret = os.environ.get("MCP_CLIENT_SECRET", "")
        tenant = required_setting("MCP_TENANT_ID") if secret else ""
        client = required_setting("MCP_CLIENT_ID") if secret else ""
        return cls(url, ca, app_id, tenant, client, secret)

    @property
    def scope(self):
        return f"api://{self.api_app_id}/.default"

    def tls_context(self):
        context = ssl.create_default_context(cafile=str(self.gateway_ca))
        context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        return context


class PoolClient:
    def __init__(self, config):
        self.config = config
        self.credential = None

    async def __aenter__(self):
        config = self.config
        self.tls = config.tls_context()
        if config.client_secret:
            self.credential = ClientSecretCredential(
                config.tenant_id, config.client_id, config.client_secret)
        else:
            self.credential = DefaultAzureCredential()
        return self

    async def __aexit__(self, *exc):
        credential, self.credential = self.credential, None
        await credential.close()

    @asynccontextmanager
    async def connection(self):
        if self.credential is None:
            raise RuntimeError("Use PoolClient in an async with block")
        token = await self.credential.get_token(self.config.scope)
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token.token}"},
            verify=self.tls, timeout=httpx.Timeout(400, connect=15), trust_env=False,
        ) as http:
            async with streamable_http_client(
                self.config.gateway_url, http_client=http,
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

    async def list_tools(self):
        async with self.connection() as session:
            return [tool.name for tool in (await session.list_tools()).tools]

    async def call(self, tool, **arguments):
        """Call once: uncertain execution must never be replayed automatically."""
        async with self.connection() as session:
            result = await session.call_tool(tool, arguments)
        text = "\n".join(item.text for item in result.content if isinstance(item, TextContent))
        if result.isError:
            raise RuntimeError(f"{tool} failed: {text}")
        if not text:
            raise ValueError(f"{tool} returned no text result")
        return text

    async def wait_until_active(self, session_id, timeout=QUEUE_WAIT_SECONDS, poll_interval=5):
        if timeout <= 0 or poll_interval <= 0:
            raise ValueError("Readiness timeout and poll interval must be positive")
        deadline = time.monotonic() + timeout
        async with asyncio.timeout(timeout):
            while time.monotonic() < deadline:
                status = json.loads(await self.call(
                    "get_session_status", session_id=session_id))["status"]
                if status == "active":
                    return
                if status in ("expired", "failed", "closed"):
                    raise RuntimeError(f"Session ended as {status}; create a new session later")
                if status not in ("queued", "activating"):
                    raise ValueError(f"Unexpected session status: {status}")
                await asyncio.sleep(poll_interval)
        raise TimeoutError("Gave up waiting for a confidential worker")

    @asynccontextmanager
    async def session(self):
        """Always release a known lease, including failed acquisition readiness."""
        session_id = json.loads(await self.call("create_session"))["session_id"]
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("create_session returned an invalid session_id")
        primary = None
        try:
            await self.wait_until_active(session_id)
            yield session_id
        except BaseException as error:
            primary = error
            raise
        finally:
            release = asyncio.create_task(self.call("delete_session", session_id=session_id))
            try:
                await asyncio.shield(release)
            except asyncio.CancelledError as cancellation:
                try:
                    await release
                except BaseException as cleanup_error:
                    raise BaseExceptionGroup(
                        "Session operation and cleanup failed",
                        [primary if primary is not None else cancellation, cleanup_error],
                    ) from None
                if primary is None:
                    raise
            except Exception as cleanup_error:
                if primary is not None:
                    raise BaseExceptionGroup(
                        "Session operation and cleanup failed", [primary, cleanup_error],
                    ) from None
                raise

    async def upload(self, session_id, source, destination="input.xlsx"):
        data = Path(source).read_bytes()
        if len(data) > 1024 * 1024:
            raise ValueError("Example upload exceeds the pilot's 1 MiB file limit")
        await self.call("upload_file", session_id=session_id, path=destination,
                        content_base64=base64.b64encode(data).decode("ascii"))

    async def download(self, session_id, source, destination):
        encoded = await self.call("download_file", session_id=session_id, path=source)
        data = base64.b64decode(encoded, validate=True)
        Path(destination).write_bytes(data)
        return Path(destination)
