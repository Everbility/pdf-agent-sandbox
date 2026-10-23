# A sandbox for running shell commands against PDF files: Python with PDF
# libraries plus poppler and qpdf. Each command runs under bubblewrap with
# one workspace directory mounted at /pdf.
FROM python:3.11-slim-trixie

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        bubblewrap poppler-utils qpdf ripgrep binutils fonts-dejavu-core ca-certificates && \
    dpkg --compare-versions "$(dpkg-query -W -f='${Version}' ca-certificates)" ge '20250419+deb13u1' && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Use the patched OS trust store, then bootstrap pip from the advisory wheel.
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_DIR=/etc/ssl/certs
RUN python -m pip install --no-cache-dir --no-index --no-deps --disable-pip-version-check --upgrade \
    "pip @ https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl#sha256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70"

# Runs as uid 1000 so a caller that shares the workspace volume can share
# files without extra permission handling.
RUN groupadd -r -g 1000 sandbox && useradd -r -u 1000 -g sandbox -m sandbox && \
    mkdir -p /var/lib/pdf-sandbox/workspaces /pdf /skills && \
    chown -R sandbox:sandbox /var/lib/pdf-sandbox /pdf

COPY --from=ghcr.io/astral-sh/uv:0.13.4@sha256:9f130421e930bfbdc563c00740466603f8723158e90297d07957c2294640262b /uv /uvx /usr/local/bin/
ENV UV_PROJECT_ENVIRONMENT=/app/.venv
ENV PATH="/app/.venv/bin:${PATH}"

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-cache

COPY server.py ./
COPY tests ./tests
RUN uv sync --locked --no-cache && \
    PDF_SHELL_INTERNAL_SECRET=test uv run --locked python -m pytest -q tests && \
    uv sync --locked --no-dev --no-cache

USER sandbox
EXPOSE 3003
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3003/healthz', timeout=2).status == 200 else 1)"]

CMD ["python", "-m", "uvicorn", "server:create_app", "--factory", "--host", "0.0.0.0", "--port", "3003"]
