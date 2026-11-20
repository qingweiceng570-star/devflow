FROM python:3.12.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_NO_CACHE=1 UV_HTTP_TIMEOUT=120
RUN pip install --no-cache-dir --timeout 120 --retries 3 uv==0.12.23
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update -o Acquire::Retries=3 \
    && apt-get install -y -o Acquire::Retries=3 --no-install-recommends git ripgrep ca-certificates
WORKDIR /workspace/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev
COPY backend/app ./app
COPY backend/alembic.ini ./
COPY backend/migrations ./migrations
COPY fixtures /workspace/fixtures
CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8100"]
