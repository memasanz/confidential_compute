import asyncio
import base64
import json
from urllib.parse import urlsplit

from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult

from auth import authenticated_owner


class Gateway:
    def __init__(self, store, transport, owner=authenticated_owner):
        self.store, self.transport, self.owner = store, transport, owner
        # Per-session keys live only in this process's memory. A gateway restart loses them, which
        # invalidates the session: a worker accepts its key once, so there is no resumption.
        self.channels = {}

    async def acquire(self):
        rows = await asyncio.to_thread(self.store.view)
        for sid in [s for s in self.channels if rows.get("s:" + s, {}).get("state") != "active"]:
            del self.channels[sid]
        return await asyncio.to_thread(self.store.acquire, self.owner())

    async def status(self, sid):
        return await asyncio.to_thread(self.store.status, sid, self.owner())

    async def listing(self):
        return await asyncio.to_thread(self.store.listing, self.owner())

    async def release(self, sid):
        self.channels.pop(sid, None)
        return await asyncio.to_thread(self.store.release, sid, self.owner())

    async def dispatch(self, sid, name, arguments):
        allowed = {"write_file", "read_file", "list_dir", "upload_file", "download_file",
                   "run_python", "pip_install", "run_command", "run_tests", "git_clone",
                   "get_attestation"}
        if name not in allowed:
            raise ValueError("Unknown routed tool")
        limits = self.store.limits
        if len(json.dumps(arguments).encode()) > limits.upload_bytes:
            raise ValueError("Input exceeds configured bound")
        if name == "upload_file":
            data = base64.b64decode(arguments["content_base64"], validate=True)
            if len(data) > limits.upload_bytes:
                raise ValueError("Upload exceeds configured bound")
        if "timeout" in arguments and not 0 < arguments["timeout"] <= limits.operation:
            raise ValueError("Tool timeout exceeds configured operation bound")
        owner = self.owner()
        worker, op, generation, remaining = await asyncio.to_thread(self.store.begin, sid, owner)
        args = dict(arguments)
        if name != "get_attestation":
            args["session_id"] = worker["lifetime"]
        try:
            async with asyncio.timeout(remaining):
                if name == "get_attestation":
                    result = await self.transport.call(worker, name, args)
                else:
                    if sid not in self.channels:
                        self.channels[sid] = await self.transport.establish(worker)
                    result = await self.transport.invoke(worker, self.channels[sid], name, args)
                if len(result.model_dump_json().encode()) > limits.result_bytes:
                    raise ValueError("Result exceeds configured bound")
        except BaseException:
            # Unknown transport outcome MUST NOT be replayed. Retire the whole group.
            self.channels.pop(sid, None)
            await asyncio.shield(asyncio.to_thread(
                self.store.finish, sid, owner, op, generation, True))
            raise RuntimeError("Worker outcome uncertain; session retired, operation not replayed") from None
        valid = await asyncio.to_thread(self.store.finish, sid, owner, op, generation)
        if not valid:
            self.channels.pop(sid, None)
            raise PermissionError("Lease closed or expired during operation; result discarded")
        return result


def build_mcp(gateway, verifier, resource_url, host="127.0.0.1", port=8000):
    limits = gateway.store.limits
    url = urlsplit(resource_url)
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise ValueError("Explicit private HTTPS gateway URL required")
    mcp = FastMCP(
        "confidential-session-gateway", host=host, port=port, stateless_http=True,
        log_level="CRITICAL",  # content-free logs: tool errors and requests may carry session data
        json_response=True, max_request_body_size=limits.upload_bytes + 65536,
        token_verifier=verifier, auth=AuthSettings(
            issuer_url=verifier.issuer, resource_server_url=resource_url,
            required_scopes=["pool.authorized"], validate_token_resource=False),
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[url.netloc], allowed_origins=[f"https://{url.netloc}"]))

    @mcp.tool()
    async def create_session(session_id: str = "") -> str:
        """Acquire an opaque session. Poll status until active; caller-chosen IDs forbidden."""
        if session_id:
            raise ValueError("Gateway generates session IDs")
        return json.dumps(await gateway.acquire())

    @mcp.tool()
    async def get_session_status(session_id: str) -> str:
        return json.dumps(await gateway.status(session_id))

    @mcp.tool()
    async def list_sessions() -> str:
        return json.dumps(await gateway.listing())

    @mcp.tool()
    async def delete_session(session_id: str) -> str:
        return json.dumps(await gateway.release(session_id))

    @mcp.tool(structured_output=False)
    async def write_file(session_id: str, path: str, content: str) -> CallToolResult:
        return await gateway.dispatch(session_id, "write_file", {"path": path, "content": content})

    @mcp.tool(structured_output=False)
    async def read_file(session_id: str, path: str) -> CallToolResult:
        return await gateway.dispatch(session_id, "read_file", {"path": path})

    @mcp.tool(structured_output=False)
    async def list_dir(session_id: str, path: str = ".") -> CallToolResult:
        return await gateway.dispatch(session_id, "list_dir", {"path": path})

    @mcp.tool(structured_output=False)
    async def upload_file(session_id: str, path: str, content_base64: str) -> CallToolResult:
        return await gateway.dispatch(session_id, "upload_file", {"path": path, "content_base64": content_base64})

    @mcp.tool(structured_output=False)
    async def download_file(session_id: str, path: str) -> CallToolResult:
        return await gateway.dispatch(session_id, "download_file", {"path": path})

    @mcp.tool(structured_output=False)
    async def run_python(session_id: str, code: str, timeout: int = limits.operation) -> CallToolResult:
        return await gateway.dispatch(session_id, "run_python", {"code": code, "timeout": timeout})

    @mcp.tool(structured_output=False)
    async def run_command(session_id: str, command: str, timeout: int = limits.operation) -> CallToolResult:
        return await gateway.dispatch(session_id, "run_command", {"command": command, "timeout": timeout})

    @mcp.tool(structured_output=False)
    async def pip_install(session_id: str, packages: str, timeout: int = limits.operation) -> CallToolResult:
        return await gateway.dispatch(session_id, "pip_install", {"packages": packages, "timeout": timeout})

    @mcp.tool(structured_output=False)
    async def run_tests(session_id: str, target: str = ".", timeout: int = limits.operation) -> CallToolResult:
        return await gateway.dispatch(session_id, "run_tests", {"target": target, "timeout": timeout})

    @mcp.tool(structured_output=False)
    async def git_clone(session_id: str, repo_url: str, directory: str = "repo") -> CallToolResult:
        return await gateway.dispatch(session_id, "git_clone", {"repo_url": repo_url, "directory": directory})

    @mcp.tool(structured_output=False)
    async def get_attestation(session_id: str, nonce: str) -> CallToolResult:
        """Evidence for this lease; not evidence for the outside-TEE gateway."""
        return await gateway.dispatch(session_id, "get_attestation", {"nonce": nonce})

    return mcp
