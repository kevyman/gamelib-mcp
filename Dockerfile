FROM python:3.12-slim
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
RUN uv sync --frozen --no-dev --no-cache
CMD ["python", "-m", "gamelib_mcp.main"]
