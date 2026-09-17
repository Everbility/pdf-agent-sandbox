"""pdf-shell contract tests. They run in isolation mode ``none`` so they pass
on developer machines without bubblewrap; the bwrap argv is checked directly."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("PDF_SHELL_INTERNAL_SECRET", "test")
os.environ["PDF_SHELL_SKIP_APP"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server
from fastapi.testclient import TestClient

HEADERS = {"X-Pdf-Shell-Secret": "test"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "WORK_ROOT", tmp_path.resolve())
    app = server.create_app(isolation="none")
    with TestClient(app) as test_client:
        yield test_client


def _exec(client, command, workspace="user_1/conv-1", timeout_ms=None):
    payload = {"execId": "e1", "workspace": workspace, "command": command}
    if timeout_ms is not None:
        payload["timeoutMs"] = timeout_ms
    return client.post("/exec", json=payload, headers=HEADERS)


def test_requires_secret(client):
    response = client.post(
        "/exec", json={"execId": "e", "workspace": "a/b", "command": "true"}
    )
    assert response.status_code == 403


def test_runs_in_the_workspace_directory(client, tmp_path):
    response = _exec(client, "pwd && echo hi > note.txt && cat note.txt")
    body = response.json()
    assert body["exitCode"] == 0
    assert body["stdout"].splitlines()[0] == str(
        (tmp_path / "user_1" / "conv-1").resolve()
    )
    assert (tmp_path / "user_1" / "conv-1" / "note.txt").read_text() == "hi\n"


def test_rejects_traversal_and_malformed_workspaces(client):
    for workspace in ("../x/y", "a", "a/b/c", "a/..", "/etc/passwd"):
        assert _exec(client, "true", workspace=workspace).status_code == 400, workspace


def test_reports_exit_code_and_stderr(client):
    body = _exec(client, "echo out; echo err >&2; exit 3").json()
    assert body == {"stdout": "out\n", "stderr": "err\n", "exitCode": 3}


def test_truncates_output(client, monkeypatch):
    monkeypatch.setattr(server, "MAX_OUTPUT_BYTES", 64)
    body = _exec(client, "yes x | head -c 500").json()
    assert body["stdout"].endswith("[output truncated at 64 bytes]")


def test_times_out(client):
    body = _exec(client, "sleep 5", timeout_ms=1000).json()
    assert body["exitCode"] == 124
    assert "timed out" in body["stderr"]


def test_healthz_reports_mode(client):
    assert client.get("/healthz").json() == {"ok": True, "isolation": "none"}


def test_bwrap_argv_mounts_only_the_workspace(tmp_path):
    workdir = tmp_path / "u" / "c"
    workdir.mkdir(parents=True)
    argv = server.build_argv("bwrap", workdir, "python3 fill.py")
    assert argv[0] == "bwrap"
    assert "--unshare-all" in argv
    assert argv[argv.index("--bind") + 1 :][:2] == [str(workdir), "/pdf"]
    assert argv[-3:] == ["/bin/bash", "-c", "python3 fill.py"]
    binds = [argv[i + 1] for i, item in enumerate(argv) if item == "--bind"]
    assert binds == [str(workdir)], "exactly one writable bind"
