"""In-VNet pilot smoke/acceptance driver, run as a Container Apps job (managed identities, no secrets).

Env: SMOKE_URL (https://<gateway-fqdn>/mcp), SMOKE_AUDIENCE, SMOKE_CLIENT_A, SMOKE_CLIENT_B (user-assigned
identity client IDs holding the API app role), SMOKE_TRUST (path to PEM, or "tofu" = pin the gateway
ingress certificate presented now and print its fingerprint), SMOKE_PHASE = basic | capacity | hostile (hostile runs adversarial generated code and prints only outcome labels).
Prints JSON event lines only: never tokens, keys or file contents beyond the synthetic result.
"""

import asyncio
import base64
import io
import json
import os
import ssl
import time
import zipfile
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
from azure.identity import ManagedIdentityCredential
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

T0 = time.time()


def log(event, **fields):
    print(json.dumps({"t": round(time.time() - T0, 1), "event": event, **fields}), flush=True)


def trust_context(url):
    mode = os.environ.get("SMOKE_TRUST", "")
    parts = urlsplit(url)
    if mode == "tofu":
        pem = ssl.get_server_certificate((parts.hostname, parts.port or 443))
        import hashlib
        der = ssl.PEM_cert_to_DER_cert(pem)
        log("gateway_cert_pinned", sha256=hashlib.sha256(der).hexdigest())
        context = ssl.create_default_context()
        context.load_verify_locations(cadata=pem)
        return context
    return ssl.create_default_context(cafile=mode or None)


def synthetic_xlsx():
    """Minimal valid workbook without openpyxl: region/units/price rows."""
    rows = [("region", "units", "price")] + [(r, u, p) for r, u, p in (
        ("east", 10, 2.5), ("west", 4, 7.0), ("east", 6, 2.5), ("north", 1, 100.0), ("west", 5, 7.0))]
    cells = []
    for i, row in enumerate(rows, 1):
        cs = []
        for j, v in enumerate(row):
            ref = "ABC"[j] + str(i)
            cs.append(f'<c r="{ref}" t="inlineStr"><is><t>{v}</t></is></c>' if isinstance(v, str)
                      else f'<c r="{ref}"><v>{v}</v></c>')
        cells.append(f'<row r="{i}">{"".join(cs)}</row>')
    parts = {
        "[Content_Types].xml": '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        "_rels/.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sales" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
        + "".join(cells) + "</sheetData></worksheet>",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class Client:
    def __init__(self, name, client_id, url, audience, context):
        self.name, self.url, self.context = name, url, context
        self.credential = ManagedIdentityCredential(client_id=client_id)
        self.audience = audience

    @asynccontextmanager
    async def session(self):
        token = self.credential.get_token(self.audience + "/.default").token
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, verify=self.context,
                                     timeout=httpx.Timeout(400, connect=15), trust_env=False) as http:
            async with streamable_http_client(self.url, http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

    async def call(self, tool, **arguments):
        async with self.session() as s:
            result = await s.call_tool(tool, arguments)
        text = result.content[0].text if result.content else ""
        return result.isError, text


async def wait_status(client, sid, wanted, timeout):
    end = time.time() + timeout
    while time.time() < end:
        error, text = await client.call("get_session_status", session_id=sid)
        status = json.loads(text)["status"] if not error else "error"
        if status in wanted:
            return status
        await asyncio.sleep(5)
    return "timeout"


async def basic(a, b):
    error, text = await a.call("create_session")
    sid = json.loads(text)["session_id"]
    log("created", session=sid[:8], error=error)
    log("activated", status=await wait_status(a, sid, {"active", "expired", "failed"}, 400))
    data = base64.b64encode(synthetic_xlsx()).decode()
    log("upload", result=await a.call("upload_file", session_id=sid, path="sales.xlsx", content_base64=data))
    code = ("import pandas as pd\n"
            "df = pd.read_excel('sales.xlsx')\n"
            "df['revenue'] = df.units * df.price\n"
            "out = df.groupby('region', as_index=False).revenue.sum().sort_values('region')\n"
            "out.to_csv('summary.csv', index=False)\n"
            "print(out.to_csv(index=False))\n")
    log("run_python", result=await a.call("run_python", session_id=sid, code=code, timeout=120))
    log("download", result=await a.call("download_file", session_id=sid, path="summary.csv"))
    # Cross-owner: identity B must be unable to see or touch A's session.
    log("cross_owner_status", result=await b.call("get_session_status", session_id=sid))
    log("cross_owner_exec", result=await b.call("run_python", session_id=sid, code="print(1)", timeout=10))
    log("cross_owner_delete", result=await b.call("delete_session", session_id=sid))
    log("cross_owner_list", result=await b.call("list_sessions"))
    log("delete", result=await a.call("delete_session", session_id=sid))
    log("post_delete_exec", result=await a.call("run_python", session_id=sid, code="print(1)", timeout=10))


async def capacity(a, b):
    count = int(os.environ.get("SMOKE_SESSIONS", "4"))
    sessions, started, seen = [], time.time(), {}
    for _ in range(count):
        error, text = await a.call("create_session")
        sessions.append(json.loads(text)["session_id"])
    log("created", count=count, sessions=[s[:8] for s in sessions])

    async def poll(limit):
        # Record the first time each session is seen in a settled state.
        deadline = time.time() + limit
        while time.time() < deadline:
            for sid in sessions:
                status = json.loads((await a.call("get_session_status", session_id=sid))[1])["status"]
                if status in ("active", "expired", "failed") and sid not in seen:
                    seen[sid] = (status, round(time.time() - started, 1))
                    log("settled", session=sid[:8], status=status, seconds=seen[sid][1])
            if all(sid in seen for sid in sessions) or not (yield_pending(seen, sessions)):
                return
            await asyncio.sleep(5)

    def yield_pending(done, ids):
        return any(s not in done for s in ids)

    await poll(420)
    held = [s for s in sessions if seen.get(s, ("",))[0] == "active"]
    log("held", active=len(held), pending=[s[:8] for s in sessions if s not in seen])
    for sid in held:
        released = time.time()
        log("release", session=sid[:8], result=await a.call("delete_session", session_id=sid))
        before = len(seen)
        await poll(420)
        log("admitted_after_release", seconds=round(time.time() - released, 1), newly_settled=len(seen) - before)
    for sid in sessions:
        log("delete", session=sid[:8], result=await a.call("delete_session", session_id=sid))


HOSTILE_PROBE = '''
import json, os, resource, socket, subprocess, sys

def attempt(fn):
    try:
        fn()
        return "allowed"
    except BaseException as error:
        return "blocked:" + type(error).__name__

def connect(host):
    socket.create_connection((host, 80), timeout=4).close()

def spawn_many():
    kids = [subprocess.Popen(["sleep", "30"]) for _ in range(2000)]
    for kid in kids:
        kid.kill()

def big_memory():
    bytearray(16 * 2**30)

def big_file():
    with open("big.bin", "wb") as f:
        f.write(b"x" * (700 * 2**20))

result = {
    "uid": os.getuid(),
    "secret_env_names": sorted(k for k in os.environ if any(s in k.upper() for s in ("KEY", "TOKEN", "TLS", "SECRET"))),
    "read_server_code": attempt(lambda: open("/app/server.py").read()),
    "read_server_environ": attempt(lambda: open("/proc/1/environ").read()),
    "metadata_service": attempt(lambda: connect("169.254.169.254")),
    "internet": attempt(lambda: connect("example.com")),
    "process_bomb": attempt(spawn_many),
    "memory_bomb": attempt(big_memory),
    "oversized_file": attempt(big_file),
}
print(json.dumps(result))
'''


async def hostile(a, b):
    """Run hostile generated code and report only outcome labels; expect uid != 0 and every attempt blocked."""
    error, text = await a.call("create_session")
    sid = json.loads(text)["session_id"]
    log("activated", status=await wait_status(a, sid, {"active", "expired", "failed"}, 400))
    log("hostile", result=await a.call("run_python", session_id=sid, code=HOSTILE_PROBE, timeout=120))
    # A second session must not see the first one's files.
    log("cross_session_read", result=await a.call("read_file", session_id="not-mine", path="big.bin"))
    log("delete", result=await a.call("delete_session", session_id=sid))


async def main():
    if os.environ.get("SMOKE_PHASE") == "net":
        import socket
        for family, _, _, _, address in socket.getaddrinfo("management.azure.com", 443, type=socket.SOCK_STREAM):
            started = time.monotonic()
            try:
                socket.create_connection(address[:2], timeout=8).close()
                log("net", address=address[0], ok=True, seconds=round(time.monotonic() - started, 2))
            except OSError as error:
                log("net", address=address[0], ok=False, seconds=round(time.monotonic() - started, 2), error=repr(error))
        return
    if os.environ.get("SMOKE_PHASE") == "claims":
        token = ManagedIdentityCredential(client_id=os.environ["SMOKE_CLIENT_A"]).get_token(
            os.environ["SMOKE_AUDIENCE"] + "/.default").token
        body = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
        log("claims", **{k: body.get(k) for k in ("aud", "iss", "ver", "roles", "idtyp", "tid")})
        return
    if os.environ.get("SMOKE_PHASE") == "dns":
        import socket
        for host in os.environ["SMOKE_HOSTS"].split(","):
            try:
                log("dns", host=host, addresses=sorted({a[4][0] for a in socket.getaddrinfo(host, 443)}))
            except OSError as error:
                log("dns", host=host, error=repr(error))
        return
    url, audience = os.environ["SMOKE_URL"], os.environ["SMOKE_AUDIENCE"]
    context = trust_context(url)
    a = Client("a", os.environ["SMOKE_CLIENT_A"], url, audience, context)
    b = Client("b", os.environ["SMOKE_CLIENT_B"], url, audience, context)
    await {"basic": basic, "capacity": capacity, "hostile": hostile}[os.environ["SMOKE_PHASE"]](a, b)
    log("done")


asyncio.run(main())
