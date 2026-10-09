FROM python:3.12.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_NO_CACHE=1
RUN pip install --no-cache-dir uv==0.12.23
WORKDIR /workspace/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev
COPY backend/app ./app
COPY backend/alembic.ini ./
COPY backend/migrations ./migrations
COPY fixtures /workspace/fixtures
CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8100"]
