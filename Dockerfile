FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y lgogdownloader && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml .
COPY steam_mcp/ steam_mcp/
RUN pip install -e .
CMD ["python", "-m", "steam_mcp.main"]
