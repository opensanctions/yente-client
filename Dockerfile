# Image for the yente-mcp server. Build from the repo root:
#
#     docker build -t yente-mcp .
#
# Runtime config (YENTE_BASE_URL, OPENSANCTIONS_API_KEY, ...) comes from env
# vars; see python/docs/mcp.md.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    YENTE_MCP_HOST=0.0.0.0

# uv and the source are bind-mounted for the install only, so neither ends up
# in the image.
RUN --mount=from=ghcr.io/astral-sh/uv:0.13,source=/uv,target=/bin/uv \
    --mount=type=bind,source=python,target=/src \
    uv pip install --system --no-cache "/src[mcp]"

RUN useradd --system --no-create-home --uid 10001 yente
USER yente

# Cloud Run passes the listen port in $PORT. The sh wrapper reads it at start
# time and appends any `docker run` arguments to the yente-mcp call.
EXPOSE 8080
ENTRYPOINT ["sh", "-c", "exec yente-mcp --port \"${PORT:-8080}\" \"$@\"", "yente-mcp"]
