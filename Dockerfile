# Digest-pinned (multi-arch index) so a retagged base image cannot change what
# ships; Dependabot's docker entry moves it forward. Tag kept for readability.
FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6
WORKDIR /app
ENV DEBIAN_FRONTEND=noninteractive \
    DATABASE_URL=file:/data/gamelib.db \
    GAMELIB_REQUIRE_ABSOLUTE_DB_PATH=1 \
    PATH="/app/.venv/bin:$PATH" \
    UV_LINK_MODE=copy
RUN apt-get update \
    && apt-get install -y --no-install-recommends lgogdownloader \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.11.20
COPY pyproject.toml uv.lock ./
COPY gamelib_mcp/ gamelib_mcp/
COPY skills/ skills/
RUN uv sync --frozen --no-dev --no-cache
# Run as a fixed non-root UID; the host data dir mounted at /data must be
# chowned to this UID (see deploy.md "Running as non-root").
RUN groupadd -g 10001 app \
    && useradd -m -u 10001 -g app app
USER app
ENV HOME=/home/app
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c 'import os, urllib.request; urllib.request.urlopen("http://127.0.0.1:%s/health" % os.getenv("PORT", "8000"), timeout=4)'
CMD ["python", "-m", "gamelib_mcp.main"]
