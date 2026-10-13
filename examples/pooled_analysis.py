"""Analyze a spreadsheet in a pooled confidential session.

Flow: create_session -> wait until active -> upload -> run_python -> download -> delete_session.
Each session is a dedicated, attested confidential worker that is destroyed when you delete it.
The worker has no internet access (no pip install, no git clone): pandas and openpyxl are preinstalled.

Settings (environment or examples/.env):
  MCP_TENANT_ID, MCP_CLIENT_ID, MCP_CLIENT_SECRET   service principal that holds the Sandbox.Access role
  MCP_API_APP_ID                                    API app registration (token audience)
  POOL_GATEWAY_URL                                  https://pool-gateway.<env-domain>/mcp
  POOL_GATEWAY_CA                                   PEM saved by pin_gateway_cert.py
  MCP_EXCEL_PATH                                    optional; defaults to sample_sales.xlsx
Without MCP_CLIENT_SECRET it falls back to DefaultAzureCredential (for example `az login`).

Run:  python pooled_analysis.py
"""

import asyncio
import base64
import json
import os
import ssl
import time
from pathlib import Path

import httpx
from azure.identity import ClientSecretCredential, DefaultAzureCredential
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

load_dotenv(override=True)

GATEWAY_URL = os.environ["POOL_GATEWAY_URL"]
GATEWAY_CA = os.environ["POOL_GATEWAY_CA"]
API_APP_ID = os.environ["MCP_API_APP_ID"]
EXCEL_PATH = Path(os.environ.get("MCP_EXCEL_PATH", Path(__file__).with_name("sample_sales.xlsx")))
QUEUE_WAIT_SECONDS = 15 * 60  # pilot queue wait is 10 min; a cold replacement takes ~4 min


def get_credential():
    if os.environ.get("MCP_CLIENT_SECRET"):
        return ClientSecretCredential(os.environ["MCP_TENANT_ID"], os.environ["MCP_CLIENT_ID"],
                                      os.environ["MCP_CLIENT_SECRET"])
    return DefaultAzureCredential()


async def call(tool, **arguments):
    """One MCP call with a fresh token. Returns the tool's text; raises on a tool error."""
    token = get_credential().get_token(f"{API_APP_ID}/.default").token
    # Trust only the pinned gateway certificate. PARTIAL_CHAIN lets a pinned leaf act as the trust anchor.
    tls = ssl.create_default_context(cafile=GATEWAY_CA)
    tls.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, verify=tls,
                                 timeout=httpx.Timeout(400, connect=15), trust_env=False) as http:
        async with streamable_http_client(GATEWAY_URL, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, arguments)
    text = result.content[0].text if result.content else ""
    if result.isError:
        raise RuntimeError(f"{tool} failed: {text}")
    return text


async def wait_until_active(session_id):
    """Poll until the session has a worker. A queued session waits for a free worker."""
    deadline = time.time() + QUEUE_WAIT_SECONDS
    last = None
    while time.time() < deadline:
        status = json.loads(await call("get_session_status", session_id=session_id))["status"]
        if status != last:
            print("session status:", status)
            last = status
        if status == "active":
            return
        if status in ("expired", "failed", "closed"):
            # Expired while queued means the pool stayed full: retry later.
            raise RuntimeError(f"Session ended as {status}; create a new session and try again")
        await asyncio.sleep(5)
    raise TimeoutError("Gave up waiting for a worker")


ANALYSIS = """
import pandas as pd
df = pd.read_excel('input.xlsx')
numeric = df.select_dtypes('number')
summary = numeric.agg(['count', 'mean', 'sum']).round(2)
summary.to_csv('summary.csv')
print(summary.to_string())
"""


async def main():
    # 1. Reserve a worker. Raises "Session queue full" if 20 people are already waiting.
    session_id = json.loads(await call("create_session"))["session_id"]
    print("created session", session_id[:8])
    try:
        # 2. Wait for a worker (immediate if one is ready, ~4 min if it must be created).
        await wait_until_active(session_id)

        # 3. Upload (1 MiB limit).
        content = base64.b64encode(EXCEL_PATH.read_bytes()).decode()
        await call("upload_file", session_id=session_id, path="input.xlsx", content_base64=content)

        # 4. Run analysis inside the confidential worker.
        print(await call("run_python", session_id=session_id, code=ANALYSIS, timeout=120))

        # 5. Download the result (1 MiB limit).
        downloaded = await call("download_file", session_id=session_id, path="summary.csv")
        Path("summary.csv").write_bytes(base64.b64decode(downloaded))  # returned as base64 text
        print("saved summary.csv")
    finally:
        # 6. Always end the session: it destroys the worker and frees the slot for others.
        print(await call("delete_session", session_id=session_id))


if __name__ == "__main__":
    asyncio.run(main())
