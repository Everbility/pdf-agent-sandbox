# pdf-agent-sandbox

A small sandbox in which an AI agent can run shell commands against PDF
files: Python 3 with pypdf, PyMuPDF, pdfplumber, pypdfium2, reportlab and
Pillow, plus poppler-utils, qpdf and ripgrep. Every command runs under
[bubblewrap](https://github.com/containers/bubblewrap) with only one
workspace directory mounted, a fresh pid/network/ipc namespace, a read-only
root filesystem and no network.

It exists so an agent can inspect, fill, render and verify PDF forms by
writing its own code, without giving it a shell on the host. It has no
knowledge of forms, fields or any application; it runs commands.

## API

`POST /exec` with header `X-Pdf-Shell-Secret: <PDF_SHELL_INTERNAL_SECRET>`:

```json
{"execId": "any-id", "workspace": "<tenant>/<session>", "command": "python3 fill.py", "timeoutMs": 120000}
```

returns `{"stdout": "...", "stderr": "...", "exitCode": 0}`. `workspace` is
two path segments under `PDF_SHELL_WORK_ROOT`; that directory is created on
first use and mounted at `/pdf` inside the sandbox, which is also the
working directory. `GET /healthz` reports the isolation mode.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PDF_SHELL_INTERNAL_SECRET` | required | Shared secret callers must present |
| `PDF_SHELL_WORK_ROOT` | `/var/lib/pdf-sandbox/workspaces` | Root holding `<tenant>/<session>` workspaces |
| `PDF_SHELL_SKILLS_DIR` | `/skills` | Optional directory of helper files for the agent, bound read-only at `/skills` when it exists |
| `PDF_SHELL_ISOLATION` | `bwrap` | `bwrap`, or `none` for a single-user development host without namespaces; `none` also requires `PDF_SHELL_ALLOW_UNISOLATED=1` and gives commands access to every workspace |
| `PDF_SHELL_DEFAULT_TIMEOUT_MS` / `PDF_SHELL_MAX_TIMEOUT_MS` | 120000 / 300000 | Per-command timeout and its cap |
| `PDF_SHELL_MAX_OUTPUT_BYTES` | 200000 | stdout/stderr cap per command |

## Running

```
docker build -t pdf-agent-sandbox .
docker run --rm -p 3003:3003 \
  --security-opt seccomp=unconfined --security-opt systempaths=unconfined \
  -e PDF_SHELL_INTERNAL_SECRET=change-me -v work:/var/lib/pdf-sandbox/workspaces pdf-agent-sandbox
```

Bubblewrap needs user namespaces and a fresh `/proc`, which Docker's default
seccomp profile and masked system paths block; the two `security_opt` flags
lift exactly those. On hosts whose AppArmor restricts unprivileged user
namespaces (Ubuntu 24.04 and later, including GitHub-hosted runners) the
probe fails with `Failed to make / slave: Permission denied`; add
`--security-opt apparmor=unconfined` there as well. Do not run the image under emulation (for example an
amd64 image on Apple Silicon): the mount syscalls bubblewrap uses are not
implemented there. Build for the host's native platform. The container
runs as uid 1000; make the work root writable by that uid.

The service listens on port 3003 and expects to be reachable only from the
caller that owns the workspaces; put it on an internal network.

Tests: `PDF_SHELL_INTERNAL_SECRET=test python -m pytest tests` (they run in
isolation mode `none` so they pass without bubblewrap; the image build runs
them too).

## Licence

GNU Affero General Public License v3.0 or later; see `LICENSE` and
`NOTICE`. The image bundles PyMuPDF, which is AGPL-licensed by Artifex.

## Maintainers

Development happens in a private repository; each commit here is a sync
from it and carries the short hash of the source commit.
