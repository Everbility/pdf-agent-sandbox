"""A sandbox for running shell commands against PDF files.

One endpoint, ``POST /exec``: run a shell command inside one workspace and
return ``{stdout, stderr, exitCode}``. The caller decides which commands to
send here; this service only runs them.

Isolation is per command, not per container. With ``PDF_SHELL_ISOLATION=bwrap``
(the default) every command runs under bubblewrap with a fresh pid/net/ipc
namespace, the image's filesystem read-only, a private /tmp, and exactly one
writable bind: the workspace directory, mounted at ``/pdf``, which is also
the working directory. ``PDF_SHELL_ISOLATION=none`` runs the command directly
with the workspace as cwd and is for development hosts where bubblewrap
cannot create namespaces; it is logged loudly at start.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("pdf-shell")
logging.basicConfig(level=logging.INFO, format="%(message)s")

INTERNAL_SECRET = os.getenv("PDF_SHELL_INTERNAL_SECRET", "").strip()
WORK_ROOT = Path(
    os.getenv("PDF_SHELL_WORK_ROOT", "/var/lib/pdf-sandbox/workspaces")
).resolve()
SKILLS_DIR = os.getenv("PDF_SHELL_SKILLS_DIR", "/skills")
ISOLATION = os.getenv("PDF_SHELL_ISOLATION", "bwrap").strip().lower()
# Mode `none` runs commands with plain access to everything the container
# can see, including every other workspace under the work root. It needs a
# second, explicit acknowledgement so it cannot be reached by a typo.
ALLOW_UNISOLATED = os.getenv("PDF_SHELL_ALLOW_UNISOLATED", "") == "1"
DEFAULT_TIMEOUT_MS = int(os.getenv("PDF_SHELL_DEFAULT_TIMEOUT_MS", "120000"))
MAX_TIMEOUT_MS = int(os.getenv("PDF_SHELL_MAX_TIMEOUT_MS", "300000"))
MAX_OUTPUT_BYTES = int(os.getenv("PDF_SHELL_MAX_OUTPUT_BYTES", "200000"))
# What the sandboxed process sees; the host environment never leaks in.
SANDBOX_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/tmp",
    "LANG": "C.UTF-8",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "MPLCONFIGDIR": "/tmp",
}

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

if not INTERNAL_SECRET:
    print(
        json.dumps(
            {
                "event": "pdf-shell.config",
                "error": "PDF_SHELL_INTERNAL_SECRET is required",
            }
        )
    )
    sys.exit(1)


def _bwrap_available() -> bool:
    """Can bubblewrap build the real sandbox on this host? Probed once at
    start with the same mounts a command gets, so a container that cannot
    unshare or mount /proc fails at boot, not on the first fill."""
    if shutil.which("bwrap") is None:
        return False
    probe_dir = Path("/tmp/pdf-shell-probe")
    probe_dir.mkdir(parents=True, exist_ok=True)
    argv = build_argv("bwrap", probe_dir, "/bin/true")
    try:
        probe = subprocess.run(argv, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if probe.returncode != 0:
        logger.error(
            json.dumps(
                {
                    "event": "pdf-shell.bwrap-probe",
                    "stderr": probe.stderr.decode("utf-8", "replace")[:300],
                }
            )
        )
    return probe.returncode == 0


def resolve_isolation(requested: str) -> str:
    if requested == "none":
        if not ALLOW_UNISOLATED:
            raise SystemExit(
                "PDF_SHELL_ISOLATION=none gives commands access to every "
                "workspace; set PDF_SHELL_ALLOW_UNISOLATED=1 as well, and only "
                "on a single-user development host"
            )
        logger.warning(
            json.dumps(
                {
                    "event": "pdf-shell.isolation",
                    "mode": "none",
                    "note": "development only",
                }
            )
        )
        return "none"
    if requested != "bwrap":
        raise SystemExit(
            f"PDF_SHELL_ISOLATION must be bwrap or none, got {requested!r}"
        )
    if not _bwrap_available():
        raise SystemExit(
            "PDF_SHELL_ISOLATION=bwrap but bubblewrap cannot create namespaces here; "
            "run the container with user namespaces allowed or set PDF_SHELL_ISOLATION=none for development"
        )
    logger.info(json.dumps({"event": "pdf-shell.isolation", "mode": "bwrap"}))
    return "bwrap"


class ExecRequest(BaseModel):
    execId: str = Field(min_length=1)
    # Two path segments, "<tenant>/<session>", under the work root.
    workspace: str
    command: str
    timeoutMs: int | None = None


class ExecResponse(BaseModel):
    stdout: str
    stderr: str
    exitCode: int


def workspace_dir(workspace: str) -> Path:
    parts = workspace.split("/")
    if len(parts) != 2 or not all(_SEGMENT_RE.match(part) for part in parts):
        raise HTTPException(status_code=400, detail="invalid_workspace")
    target = (WORK_ROOT / parts[0] / parts[1]).resolve()
    if WORK_ROOT not in target.parents:
        raise HTTPException(status_code=400, detail="invalid_workspace")
    target.mkdir(parents=True, exist_ok=True)
    return target


def build_argv(mode: str, workdir: Path, command: str) -> list[str]:
    if mode == "none":
        return ["/bin/bash", "-c", command]
    argv = [
        "bwrap",
        "--die-with-parent",
        "--unshare-all",
        "--new-session",
        "--clearenv",
    ]
    for key, value in SANDBOX_ENV.items():
        argv += ["--setenv", key, value]
    for root in ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc"):
        if Path(root).exists():
            argv += ["--ro-bind", root, root]
    argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    argv += ["--bind", str(workdir), "/pdf"]
    if Path(SKILLS_DIR).is_dir():
        argv += ["--ro-bind", SKILLS_DIR, "/skills"]
    argv += ["--chdir", "/pdf", "--", "/bin/bash", "-c", command]
    return argv


def _truncate(data: bytes) -> str:
    text = data[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    if len(data) <= MAX_OUTPUT_BYTES:
        return text
    return text + f"\n[output truncated at {MAX_OUTPUT_BYTES} bytes]"


async def run_command(
    mode: str, workdir: Path, command: str, timeout_ms: int
) -> ExecResponse:
    process = await asyncio.create_subprocess_exec(
        *build_argv(mode, workdir, command),
        cwd=str(workdir),
        # bwrap clears and sets its own environment; only the direct mode
        # needs the host's kept out here.
        env=dict(SANDBOX_ENV) if mode == "none" else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout_ms / 1000
        )
    except TimeoutError:
        # start_new_session made the child its own process group leader, so
        # this takes the whole pipeline with it.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        await process.wait()
        return ExecResponse(
            stdout="",
            stderr=f"bash: command timed out after {timeout_ms // 1000}s",
            exitCode=124,
        )
    return ExecResponse(
        stdout=_truncate(stdout),
        stderr=_truncate(stderr),
        exitCode=int(process.returncode or 0),
    )


def create_app(*, isolation: str | None = None) -> FastAPI:
    mode = resolve_isolation(isolation or ISOLATION)
    app = FastAPI(title="pdf-shell")

    def require_secret(
        x_pdf_shell_secret: Annotated[
            str | None, Header(alias="X-Pdf-Shell-Secret")
        ] = None,
    ) -> None:
        supplied = str(x_pdf_shell_secret or "").encode("utf-8")
        if not hmac.compare_digest(supplied, INTERNAL_SECRET.encode("utf-8")):
            raise HTTPException(status_code=403, detail="forbidden")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "isolation": mode}

    @app.post(
        "/exec", response_model=ExecResponse, dependencies=[Depends(require_secret)]
    )
    async def exec_command(payload: ExecRequest) -> ExecResponse:
        workdir = workspace_dir(payload.workspace)
        timeout_ms = min(
            max(int(payload.timeoutMs or DEFAULT_TIMEOUT_MS), 1000), MAX_TIMEOUT_MS
        )
        command = payload.command.strip()
        if not command:
            return ExecResponse(stdout="", stderr="", exitCode=0)
        result = await run_command(mode, workdir, command, timeout_ms)
        logger.info(
            json.dumps(
                {
                    "event": "pdf-shell.exec",
                    "exec_id": payload.execId,
                    "exit_code": result.exitCode,
                    "stdout_bytes": len(result.stdout.encode("utf-8")),
                    "stderr_bytes": len(result.stderr.encode("utf-8")),
                }
            )
        )
        return result

    return app
