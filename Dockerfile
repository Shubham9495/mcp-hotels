FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
# Optional extra root CAs for networks with TLS inspection (drop *.crt files in docker/extra-ca/).
COPY docker/extra-ca/ /usr/local/share/ca-certificates/extra/
RUN update-ca-certificates >/dev/null 2>&1 || true
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY supplier ./supplier
COPY mcp_server ./mcp_server
COPY dashboard ./dashboard
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"
