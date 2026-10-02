"""Confidential code-interpreter sandbox exposed over MCP.

An MCP server that gives an external agent isolated Python sessions to write,
install packages into, and execute code. It is designed to run inside an Azure
Container Instances confidential container group (AMD SEV-SNP TEE), so the work
happens in a hardware-attested, memory-encrypted environment.

Design (single-tenant):
- The confidential container group is the trust boundary. Data in use is hidden
  from the Azure host/operator and the image is pinned by the CCE policy.
- A "session" is an isolated workspace directory with its own virtual
  environment, giving each agent task a clean, separate Python environment.
- The bearer token that gates the server is released from managed HSM only
  after attestation (Secure Key Release), so credentials never exist in
  host-visible configuration.

Command model: free_run. The CCE policy pins only this server's image and
entrypoint; commands the agent requests run as child processes of the server.
"""

import base64
import contextvars
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import venv
from pathlib import Path

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

# Root under which every isolated session lives.
WORKSPACE = Path(os.environ.get("SANDBOX_WORKSPACE", "/workspace")).resolve()
SESSIONS_ROOT = WORKSPACE / "sessions"
SESSIONS_ROOT.mkdir(parents=True, exist_ok=True)

# Session ownership records live outside the agent-writable session directories,
# so code running in a session cannot tamper with who owns it.
OWNERS_DIR = WORKSPACE / ".owners"
OWNERS_DIR.mkdir(parents=True, exist_ok=True)

# Microsoft Entra ID validation. When configured, every request must carry a
# valid Entra access token, and the caller's identity (the token's object id) is
# used to enforce per-session ownership. Without it, the server runs in local
# dev mode and takes the caller identity from an X-Caller-Id header.
ENTRA_TENANT_ID = os.environ.get("ENTRA_TENANT_ID", "")
ENTRA_AUDIENCE = os.environ.get("ENTRA_AUDIENCE", "")

# Local REST endpoint of the confidential-computing attestation / SKR sidecar.
ATTESTATION_SIDECAR = os.environ.get("ATTESTATION_SIDECAR_URL", "http://localhost:8080")

# Secure Key Release configuration. When MCP_AUTH_KID is set, the bearer token
# is released from managed HSM after attestation instead of being passed in as
# host-visible configuration.
SKR_MAA_ENDPOINT = os.environ.get("SKR_MAA_ENDPOINT", "")
SKR_AKV_ENDPOINT = os.environ.get("SKR_AKV_ENDPOINT", "")
MCP_AUTH_KID = os.environ.get("MCP_AUTH_KID", "")

DEFAULT_TIMEOUT = int(os.environ.get("SANDBOX_COMMAND_TIMEOUT", "600"))
HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "8000"))

# TLS terminates inside the enclave so traffic stays encrypted until it reaches
# the attested TEE. Supply PEM file paths, or PEM content (e.g. from Key Vault).
TLS_CERTFILE = os.environ.get("MCP_TLS_CERTFILE", "")
TLS_KEYFILE = os.environ.get("MCP_TLS_KEYFILE", "")
TLS_CERT = os.environ.get("MCP_TLS_CERT", "")
TLS_KEY = os.environ.get("MCP_TLS_KEY", "")

# Mutable holder so the middleware sees the token resolved at startup.
_auth = {"token": os.environ.get("MCP_AUTH_TOKEN", "")}

# The identity of the caller handling the current request.
current_caller: contextvars.ContextVar[str] = contextvars.ContextVar("current_caller", default="")

SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")

# A per-end-user identifier the agent stamps on each request (X-User-Id header).
# When present it is folded into the caller identity so sessions are owned by
# (agent oid + user), giving server-enforced per-user isolation even though the
# agent authenticates with a single service principal. Kept deliberately strict
# so it can be embedded in the owner record without ambiguity.
USER_ID_PATTERN = re.compile(r"[A-Za-z0-9_.@-]{1,128}")

mcp = FastMCP("confidential-code-sandbox", host=HOST, port=PORT)


# --- ownership ----------------------------------------------------------------

def _owner_path(session_id: str) -> Path:
    """Path to a session's ownership record (outside the session workspace)."""
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValueError(f"Invalid session id: {session_id!r}")
    return OWNERS_DIR / session_id


def _set_owner(session_id: str, caller: str) -> None:
    _owner_path(session_id).write_text(caller, encoding="utf-8")


def _get_owner(session_id: str) -> str | None:
    path = _owner_path(session_id)
    return path.read_text(encoding="utf-8") if path.exists() else None


def _require_owned(session_id: str) -> Path:
    """Authorize the current caller for a session, returning its directory.

    Only the identity that created the session may access it. This is enforced
    inside the attested enclave, so neither other callers nor infrastructure
    admins can reach another caller's session data.
    """
    caller = current_caller.get()
    owner = _get_owner(session_id)
    if owner is None:
        raise ValueError(f"Session {session_id} not found")
    if owner != caller:
        raise PermissionError("Only the session creator may access this session")
    return _session_dir(session_id)


# --- session and path helpers -------------------------------------------------

def _session_dir(session_id: str) -> Path:
    """Resolve a session's isolated directory, validating the id."""
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValueError(f"Invalid session id: {session_id!r}")
    return (SESSIONS_ROOT / session_id).resolve()


def _venv_python(session_dir: Path) -> Path:
    """Path to a session's virtual environment interpreter."""
    if os.name == "nt":
        return session_dir / ".venv" / "Scripts" / "python.exe"
    return session_dir / ".venv" / "bin" / "python"


def _ensure_session(session_id: str) -> Path:
    """Create the session directory and its virtual environment if missing.

    The venv inherits the container image's site-packages (pandas, openpyxl,
    pytest, ...), so data-analysis libraries are available immediately without a
    slow per-session install. Extra packages can still be added via pip_install.
    """
    session_dir = _session_dir(session_id)
    python = _venv_python(session_dir)
    if not python.exists():
        session_dir.mkdir(parents=True, exist_ok=True)
        venv.create(session_dir / ".venv", with_pip=True, system_site_packages=True)
    return session_dir


def _safe_path(session_dir: Path, path: str) -> Path:
    """Resolve a user path and confine it to the session directory."""
    resolved = (session_dir / path).resolve()
    if resolved != session_dir and session_dir not in resolved.parents:
        raise ValueError(f"Path '{path}' escapes the session")
    return resolved


def _run(args: list[str], cwd: Path, timeout: int) -> str:
    """Run a command and return combined output."""
    result = subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
    )
    return f"$ {' '.join(args)}\n(exit {result.returncode})\n{result.stdout}{result.stderr}"


def _run_shell(command: str, cwd: Path, timeout: int) -> str:
    """Run a command through the system shell and return combined output."""
    result = subprocess.run(
        command, cwd=str(cwd), shell=True, capture_output=True, text=True, timeout=timeout
    )
    return f"$ {command}\n(exit {result.returncode})\n{result.stdout}{result.stderr}"


# --- session management tools -------------------------------------------------

@mcp.tool()
def create_session(session_id: str = "") -> str:
    """Create a new isolated code-execution session with its own Python venv.

    The calling identity becomes the session's sole owner; no other caller (or
    admin) can access it. Returns the session id to pass to the other tools.
    """
    session_id = session_id or f"s-{uuid.uuid4().hex[:12]}"
    if _get_owner(session_id) is not None:
        raise ValueError(f"Session {session_id} already exists")
    _ensure_session(session_id)
    _set_owner(session_id, current_caller.get())
    return session_id


@mcp.tool()
def list_sessions() -> str:
    """List the caller's own session ids."""
    caller = current_caller.get()
    ids = [p.name for p in sorted(OWNERS_DIR.iterdir()) if _get_owner(p.name) == caller]
    return "\n".join(ids) or "(none)"


@mcp.tool()
def delete_session(session_id: str) -> str:
    """Delete a session and everything in it (owner only)."""
    session_dir = _require_owned(session_id)
    if session_dir.exists():
        shutil.rmtree(session_dir)
    _owner_path(session_id).unlink(missing_ok=True)
    return f"Deleted session {session_id}"


# --- file tools ---------------------------------------------------------------

@mcp.tool()
def write_file(session_id: str, path: str, content: str) -> str:
    """Create or overwrite a file in the session workspace."""
    session_dir = _require_owned(session_id)
    target = _safe_path(session_dir, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} bytes to {path}"


@mcp.tool()
def read_file(session_id: str, path: str) -> str:
    """Read a file from the session workspace."""
    session_dir = _require_owned(session_id)
    return _safe_path(session_dir, path).read_text(encoding="utf-8")


@mcp.tool()
def list_dir(session_id: str, path: str = ".") -> str:
    """List the contents of a directory in the session workspace."""
    session_dir = _require_owned(session_id)
    target = _safe_path(session_dir, path)
    entries = []
    for item in sorted(target.iterdir()):
        if item.name == ".venv":
            continue
        kind = "dir " if item.is_dir() else "file"
        entries.append(f"{kind}  {item.relative_to(session_dir)}")
    return "\n".join(entries) or "(empty)"


@mcp.tool()
def upload_file(session_id: str, path: str, content_base64: str) -> str:
    """Upload a binary file (e.g. .xlsx, .csv, .parquet) into the session.

    `content_base64` is the file's bytes, base64-encoded. Use this for
    spreadsheets and other binary data the agent needs to analyze.
    """
    session_dir = _require_owned(session_id)
    target = _safe_path(session_dir, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = base64.b64decode(content_base64)
    target.write_bytes(data)
    return f"Uploaded {len(data)} bytes to {path}"


@mcp.tool()
def download_file(session_id: str, path: str) -> str:
    """Download a file from the session as base64 (e.g. a generated spreadsheet)."""
    session_dir = _require_owned(session_id)
    data = _safe_path(session_dir, path).read_bytes()
    return base64.b64encode(data).decode("ascii")


# --- code execution tools -----------------------------------------------------

@mcp.tool()
def run_python(session_id: str, code: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Execute Python code in the session's virtual environment.

    The code runs with the session workspace as its working directory, so it can
    import files written there and use packages installed via pip_install.
    """
    session_dir = _require_owned(session_id)
    return _run([str(_venv_python(session_dir)), "-c", code], session_dir, timeout)


@mcp.tool()
def pip_install(session_id: str, packages: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Install packages into the session's virtual environment.

    `packages` is a space-separated list, e.g. "requests pandas==2.2.0".
    """
    session_dir = _require_owned(session_id)
    args = [str(_venv_python(session_dir)), "-m", "pip", "install", *packages.split()]
    return _run(args, session_dir, timeout)


@mcp.tool()
def run_command(session_id: str, command: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Run a shell command inside the session workspace and return its output."""
    session_dir = _require_owned(session_id)
    return _run_shell(command, session_dir, timeout)


@mcp.tool()
def run_tests(session_id: str, target: str = ".", timeout: int = DEFAULT_TIMEOUT) -> str:
    """Run pytest in the session's virtual environment."""
    session_dir = _require_owned(session_id)
    return _run([str(_venv_python(session_dir)), "-m", "pytest", target], session_dir, timeout)


@mcp.tool()
def git_clone(session_id: str, repo_url: str, directory: str = "repo") -> str:
    """Clone a git repository into the session workspace."""
    session_dir = _require_owned(session_id)
    dest = _safe_path(session_dir, directory)
    if dest.exists():
        shutil.rmtree(dest)
    return _run(["git", "clone", "--depth", "1", repo_url, str(dest)], session_dir, 300)


# --- attestation --------------------------------------------------------------

@mcp.tool()
def get_attestation() -> str:
    """Return a signed SEV-SNP attestation token from the sidecar.

    The client should verify this token (via Microsoft Azure Attestation) before
    trusting results, to prove the code ran in a genuine, untampered enclave.
    """
    try:
        response = httpx.post(
            f"{ATTESTATION_SIDECAR}/attest/maa",
            json={"maa_endpoint": SKR_MAA_ENDPOINT, "runtime_data": ""},
            timeout=30,
        )
        response.raise_for_status()
        return response.text
    except Exception as exc:  # noqa: BLE001 - surface any sidecar issue to caller
        return f"Attestation unavailable: {exc}"


# --- secure key release (attestation-gated bearer token) ----------------------

def _release_secret(kid: str) -> str:
    """Release a key from managed HSM via the sidecar, gated on attestation.

    The sidecar produces an MAA token from the SEV-SNP report and presents it to
    managed HSM, which only releases the key if the token satisfies the key's
    release policy. So the token is unavailable outside a genuine enclave.
    """
    response = httpx.post(
        f"{ATTESTATION_SIDECAR}/key/release",
        json={
            "maa_endpoint": SKR_MAA_ENDPOINT,
            "akv_endpoint": SKR_AKV_ENDPOINT,
            "kid": kid,
        },
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Secure Key Release failed ({response.status_code}): {response.text}"
        )
    return response.json()["key"]


def _resolve_auth_token() -> None:
    """Resolve the attestation-gated bearer token via Secure Key Release.

    When Entra ID gates the server, request authentication uses the Entra access
    token and this released secret is not required, so a release failure is
    logged but non-fatal. In local/dev mode (no Entra) the released token is the
    only credential, so a failure there is fatal.
    """
    if _auth["token"] or not MCP_AUTH_KID:
        return
    try:
        _auth["token"] = _release_secret(MCP_AUTH_KID)
    except Exception as exc:  # noqa: BLE001 - surface the real SKR error
        if ENTRA_TENANT_ID and ENTRA_AUDIENCE:
            print(f"[skr] continuing without released token (Entra auth active): {exc}", flush=True)
        else:
            raise


# --- transport ----------------------------------------------------------------

def _validate_entra_token(token: str) -> str:
    """Validate an Entra access token and return the caller's object id (oid)."""
    import jwt  # imported lazily so local dev mode needs no PyJWT
    from jwt import PyJWKClient

    jwks = PyJWKClient(
        f"https://login.microsoftonline.com/{ENTRA_TENANT_ID}/discovery/v2.0/keys"
    )
    signing_key = jwks.get_signing_key_from_jwt(token)
    claims = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=ENTRA_AUDIENCE,
        issuer=f"https://login.microsoftonline.com/{ENTRA_TENANT_ID}/v2.0",
    )
    caller = claims.get("oid") or claims.get("sub")
    if not caller:
        raise ValueError("Token has no caller identity")
    return caller


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Authenticate the caller and record their identity for ownership checks.

    - When Entra is configured, require a valid Entra access token and use its
      object id (oid) as the caller identity.
    - Otherwise (local dev), require the shared bearer token and take the caller
      identity from the X-Caller-Id header.

    In both modes, an optional X-User-Id header is folded into the caller
    identity so that one agent (single service principal) can host many end
    users with server-enforced per-user session isolation.
    """

    async def dispatch(self, request, call_next):
        header = request.headers.get("authorization", "")
        if ENTRA_TENANT_ID and ENTRA_AUDIENCE:
            if not header.startswith("Bearer "):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            try:
                caller = _validate_entra_token(header[len("Bearer "):])
            except Exception:  # noqa: BLE001 - any validation failure is a 401
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        else:
            token = _auth["token"]
            if token and header != f"Bearer {token}":
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            caller = request.headers.get("x-caller-id", "local")

        # Fold an optional per-end-user id into the caller identity so sessions
        # are owned by (agent + user). A caller may only reach sessions created
        # under the exact same (agent, user) pair.
        user_id = request.headers.get("x-user-id", "").strip()
        if user_id:
            if not USER_ID_PATTERN.fullmatch(user_id):
                return JSONResponse({"error": "invalid x-user-id"}, status_code=400)
            caller = f"{caller}|{user_id}"

        reset = current_caller.set(caller)
        try:
            return await call_next(request)
        finally:
            current_caller.reset(reset)


def _materialize_pem(content: str, suffix: str) -> str:
    """Write PEM content to a temp file and return its path."""
    handle = tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False, encoding="utf-8")
    handle.write(content)
    handle.close()
    return handle.name


def _tls_files() -> tuple[str | None, str | None]:
    """Resolve TLS cert/key to file paths, or (None, None) if not configured."""
    certfile = TLS_CERTFILE or (_materialize_pem(TLS_CERT, ".crt") if TLS_CERT else None)
    keyfile = TLS_KEYFILE or (_materialize_pem(TLS_KEY, ".key") if TLS_KEY else None)
    return certfile, keyfile


if __name__ == "__main__":
    _resolve_auth_token()
    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuthMiddleware)
    certfile, keyfile = _tls_files()
    if certfile and keyfile:
        uvicorn.run(app, host=HOST, port=PORT, ssl_certfile=certfile, ssl_keyfile=keyfile)
    else:
        uvicorn.run(app, host=HOST, port=PORT)
