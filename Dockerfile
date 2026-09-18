# A sandbox for running shell commands against PDF files: Python with PDF
# libraries plus poppler and qpdf. Each command runs under bubblewrap with
# one workspace directory mounted at /pdf.
FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        bubblewrap poppler-utils qpdf ripgrep binutils fonts-dejavu-core && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Runs as uid 1000 so a caller that shares the workspace volume can share
# files without extra permission handling.
RUN groupadd -r -g 1000 sandbox && useradd -r -u 1000 -g sandbox -m sandbox && \
    mkdir -p /var/lib/pdf-sandbox/workspaces /pdf /skills && \
    chown -R sandbox:sandbox /var/lib/pdf-sandbox /pdf

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py ./
COPY tests ./tests
RUN pip install --no-cache-dir pytest==8.4.2 httpx==0.28.1 && \
    PDF_SHELL_INTERNAL_SECRET=test python -m pytest -q tests && \
    pip uninstall -y pytest httpx >/dev/null

USER sandbox
EXPOSE 3003
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3003/healthz', timeout=2).status == 200 else 1)"]

CMD ["python", "-m", "uvicorn", "server:create_app", "--factory", "--host", "0.0.0.0", "--port", "3003"]
