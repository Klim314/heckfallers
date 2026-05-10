FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY server ./server

EXPOSE 8800

CMD ["uvicorn", "server.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8800", \
     "--reload", \
     "--reload-dir", "/app/server"]
