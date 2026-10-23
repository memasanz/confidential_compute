"""MCP execution worker. Separate confidential ACI groups are the OS boundary.

The worker serves exactly one lifetime session, then requires controller group
deletion. Workspace directories/venvs are NOT hostile-code isolation: arbitrary
child processes share the worker's permissions, files and environment.
"""

import base64
import contextvars
import os
import re
import shutil
import ssl
import subprocess
import uuid
import venv
from pathlib import Path

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from pool_worker import LifetimeSession, canonical_challenge, validate_tls

# Root for workspaces; these directories are not a security boundary.
WORKSPACE = Path(os.environ.get("SANDBOX_WORKSPACE", "/workspace")).resolve()
SESSIONS_ROOT = WORKSPACE / "sessions"
SESSIONS_ROOT.mkdir(parents=True, exist_ok=True)

# Local REST endpoint of the confidential-computing attestation sidecar
# (image mcr.microsoft.com/aci/skr; used only to produce attestation reports).
ATTESTATION_SIDECAR = os.environ.get("ATTESTATION_SIDECAR_URL", "http://localhost:8080")

# MAA hostname the sidecar attests against. The variable name is historical but
# kept because the deployed container-group policy pins it.
SKR_MAA_ENDPOINT = os.environ.get("SKR_MAA_ENDPOINT", "")

DEFAULT_TIMEOUT = int(os.environ.get("SANDBOX_COMMAND_TIMEOUT", "600"))
HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "8000"))

# TLS terminates inside the enclave so traffic stays encrypted until it reaches
# the attested TEE. Supply PEM file paths, or PEM content (e.g. from Key Vault).
TLS_CERTFILE = os.environ.get("MCP_TLS_CERTFILE", "")
TLS_KEYFILE = os.environ.get("MCP_TLS_KEYFILE", "")
TLS_CERT = os.environ.get("MCP_TLS_CERT", "")
TLS_KEY = os.environ.get("MCP_TLS_KEY", "")
TLS_CLIENT_CA = os.environ.get("MCP_TLS_CLIENT_CA", "")
TLS_CLIENT_CAFILE = os.environ.get("MCP_TLS_CLIENT_CAFILE", "")
WORKER_HOSTNAME = os.environ.get("MCP_WORKER_HOSTNAME", "")
if os.environ.get("MCP_WORKER_MODE", "pooled") != "pooled":
    raise ValueError("Only pooled mode is supported (MCP_WORKER_MODE=pooled)")
pool = LifetimeSession(WORKSPACE, os.environ.get("MCP_WORKER_ID", ""),
                       os.environ.get("MCP_SESSION_ID", ""))
TLS_SPKI_SHA256 = ""

# The identity of the caller handling the current request.
current_caller: contextvars.ContextVar[str] = contextvars.ContextVar("current_caller", default="")

SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")

mcp = FastMCP("confidential-code-sandbox", host=HOST, port=PORT)


# --- authorization ------------------------------------------------------------

def _require_owned(session_id: str) -> Path:
    """API authorization only; arbitrary code within this group can bypass it."""
    _require_gateway()
    pool.require(session_id)
    return _session_dir(session_id)


# --- session and path helpers -------------------------------------------------

def _session_dir(session_id: str) -> Path:
    """Resolve a workspace directory, validating the id (not OS isolation)."""
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
        _own_tree(session_dir)
    return session_dir


def _safe_path(session_dir: Path, path: str) -> Path:
    """Resolve a user path and confine it to the session directory."""
    resolved = (session_dir / path).resolve()
    if resolved != session_dir and session_dir not in resolved.parents:
        raise ValueError(f"Path '{path}' escapes the session")
    return resolved


def _child_env(cwd: Path) -> dict[str, str]:
    """Minimal environment for generated code: no TLS keys, tokens or worker config."""
    return {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(cwd), "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1"}


SANDBOX_UID = int(os.environ.get("SANDBOX_UID", "10001"))
MAX_WORKSPACE_BYTES = int(os.environ.get("SANDBOX_MAX_WORKSPACE_BYTES", str(2 * 2**30)))


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _own(*paths: Path) -> None:
    """Give paths to the sandbox user so generated code can use them (no-op unless root)."""
    if _is_root():
        for path in paths:
            os.chown(path, SANDBOX_UID, SANDBOX_UID, follow_symlinks=False)


def _own_tree(root: Path) -> None:
    _own(root, *root.rglob("*"))


def _check_disk(session_dir: Path) -> None:
    """Fail when the workspace exceeds its size cap (the workspace is memory-backed in the TEE)."""
    total = sum(p.stat().st_size for p in session_dir.rglob("*") if p.is_file() and not p.is_symlink())
    if total > MAX_WORKSPACE_BYTES:
        raise ValueError("Workspace size limit exceeded")


def _limit_child() -> None:
    """Applied in the child before exec (Linux only): drop root, bound memory, processes, file size."""
    import resource
    if _is_root():
        os.setgroups([])
        os.setgid(SANDBOX_UID)
        os.setuid(SANDBOX_UID)
    resource.setrlimit(resource.RLIMIT_AS, (3 * 2**30, 3 * 2**30))
    resource.setrlimit(resource.RLIMIT_NPROC, (256, 256))
    resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 2**20, 512 * 2**20))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.setsid()


def _run(args: list[str], cwd: Path, timeout: int) -> str:
    """Run a command and return combined output."""
    result = subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
        env=_child_env(cwd), preexec_fn=_limit_child if os.name == "posix" else None,
    )
    _check_disk(cwd)
    return f"$ {' '.join(args)}\n(exit {result.returncode})\n{result.stdout}{result.stderr}"


def _run_shell(command: str, cwd: Path, timeout: int) -> str:
    """Run a command through the system shell and return combined output."""
    result = subprocess.run(
        command, cwd=str(cwd), shell=True, capture_output=True, text=True, timeout=timeout,
        env=_child_env(cwd), preexec_fn=_limit_child if os.name == "posix" else None,
    )
    _check_disk(cwd)
    return f"$ {command}\n(exit {result.returncode})\n{result.stdout}{result.stderr}"


# --- session management tools -------------------------------------------------

@mcp.tool()
def create_session(session_id: str = "") -> str:
    """Create the workspace/venv; consumes the worker's sole lifetime session."""
    _require_gateway()
    session_id = session_id or pool.session_id
    pool.require(session_id, active=False)
    pool.transition("active")
    try:
        _ensure_session(session_id)
    except Exception:
        pool.transition("retired")
        raise
    return session_id


@mcp.tool()
def list_sessions() -> str:
    """List this worker's session id, if active."""
    _require_gateway()
    return pool.session_id if pool.require(active=False) == "active" else "(none)"


@mcp.tool()
def delete_session(session_id: str) -> str:
    """Retire the session and remove its workspace; does NOT delete the ACI group."""
    session_dir = _require_owned(session_id)
    pool.transition("retired")
    if session_dir.exists():
        shutil.rmtree(session_dir)
    return f"Retired session {session_id}; controller must destroy the Azure group"


# --- file tools ---------------------------------------------------------------

@mcp.tool()
def write_file(session_id: str, path: str, content: str) -> str:
    """Create or overwrite a file in the session workspace."""
    session_dir = _require_owned(session_id)
    target = _safe_path(session_dir, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _own(*[p for p in (target, *target.parents) if p == session_dir or session_dir in p.parents])
    _check_disk(session_dir)
    return f"Wrote {len(content)} bytes to {path}"


@mcp.tool()
def read_file(session_id: str, path: str) -> str:
    """Read a file from the session workspace."""
    session_dir = _require_owned(session_id)
    return _safe_path(session_dir, path).read_text(encoding="utf-8")


@mcp.tool()
def delete_file(session_id: str, path: str) -> str:
    """Delete a file from the session workspace."""
    session_dir = _require_owned(session_id)
    target = _safe_path(session_dir, path)
    if target == session_dir or not target.is_file():
        raise ValueError(f"'{path}' is not a file in the session")
    target.unlink()
    return f"Deleted {path}"


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
    _own(*[p for p in (target, *target.parents) if p == session_dir or session_dir in p.parents])
    _check_disk(session_dir)
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


@mcp.tool()
def get_status() -> str:
    """Report the worker lifecycle state only; no session content."""
    _require_gateway()
    return pool.require(active=False)


# --- attestation --------------------------------------------------------------

@mcp.tool()
def get_attestation(nonce: str = "") -> str:
    """Obtain evidence, not proof of analysis correctness. Clients must verify it.

    Pooled evidence binds a fresh client challenge to this TLS public key.
    Authenticated pre-allocation attestation is allowed before activation.
    """
    _require_gateway()
    pool.require(active=False)
    runtime_data = base64.b64encode(canonical_challenge(
        pool.worker_id, pool.session_id, nonce, TLS_SPKI_SHA256
    )).decode("ascii")
    try:
        response = httpx.post(
            f"{ATTESTATION_SIDECAR}/attest/maa",
            json={"maa_endpoint": SKR_MAA_ENDPOINT, "runtime_data": runtime_data},
            timeout=30,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result.get("token"), str) or not result["token"]:
            raise RuntimeError("Sidecar did not return an MAA token")
        return response.text
    except Exception as exc:  # noqa: BLE001 - any sidecar issue means untrusted
        raise RuntimeError("Attestation failed; worker must not be trusted") from exc


# --- transport ----------------------------------------------------------------

class GatewayAuthMiddleware(BaseHTTPMiddleware):
    """Mark every request as coming from the gateway.

    The app is exposed ONLY through uvicorn's CERT_REQUIRED listener, so a request
    that reaches it already presented a client cert from the gateway CA. No
    forwarded identity headers or client-supplied caller IDs are honored.
    """

    async def dispatch(self, request, call_next):
        reset = current_caller.set("gateway-mtls")
        try:
            return await call_next(request)
        finally:
            current_caller.reset(reset)


def _materialize_pem(content: str, suffix: str) -> str:
    """Materialize configured PEM inside the worker workspace, never OS temp."""
    directory = WORKSPACE / ".transport"
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / f"{uuid.uuid4().hex}{suffix}"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
    return str(path)


def _tls_files() -> tuple[str | None, str | None]:
    """Resolve TLS cert/key to file paths, or (None, None) if not configured."""
    certfile = TLS_CERTFILE or (_materialize_pem(TLS_CERT, ".crt") if TLS_CERT else None)
    keyfile = TLS_KEYFILE or (_materialize_pem(TLS_KEY, ".key") if TLS_KEY else None)
    return certfile, keyfile


def _require_gateway():
    if current_caller.get() != "gateway-mtls":
        raise PermissionError("Authenticated gateway mTLS request required")


def transport_options():
    global TLS_SPKI_SHA256
    certfile, keyfile = _tls_files()
    if not re.fullmatch(r"[A-Za-z0-9.-]+", SKR_MAA_ENDPOINT) or "." not in SKR_MAA_ENDPOINT:
        raise ValueError("SKR_MAA_ENDPOINT must be a hostname only")
    cafile = TLS_CLIENT_CAFILE or (
        _materialize_pem(TLS_CLIENT_CA, ".ca") if TLS_CLIENT_CA else None)
    TLS_SPKI_SHA256 = validate_tls(certfile, keyfile, cafile, WORKER_HOSTNAME)
    return dict(ssl_certfile=certfile, ssl_keyfile=keyfile,
                ssl_ca_certs=cafile, ssl_cert_reqs=ssl.CERT_REQUIRED,
                proxy_headers=False)


if __name__ == "__main__":
    # Content-free logs: tool errors can embed paths, file bytes or output, so only
    # critical failures and no per-request lines reach the container log.
    import logging
    logging.getLogger("mcp").setLevel(logging.CRITICAL)
    options = transport_options()
    app = mcp.streamable_http_app()
    app.add_middleware(GatewayAuthMiddleware)
    uvicorn.run(app, host=HOST, port=PORT, access_log=False, log_level="critical", **options)
