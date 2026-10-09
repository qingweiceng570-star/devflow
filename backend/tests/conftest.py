import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.domain.contracts import RepoScope
from app.main import create_app
from app.persistence.database import Base
from app.services.auth_service import create_admin
from app.tools.registry import FIXTURE_REPO_ID

PASSWORD = "test-only-password-123"


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        app_env="test",
        database_url="sqlite://",
        llm_provider="openai_compatible",
        storage_root=tmp_path,
    )


@pytest.fixture
def scope():
    return RepoScope(actor_id=uuid4(), repo_id=FIXTURE_REPO_ID)


@pytest.fixture
def database():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        create_admin(db, "admin", PASSWORD)
    yield factory
    engine.dispose()


@pytest.fixture
def client(settings, database):
    with TestClient(create_app(settings, session_factory=database)) as client:
        yield client


@pytest.fixture
def logged_in(client):
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": PASSWORD},
        headers={"Origin": "http://localhost:3100"},
    )
    assert response.status_code == 200
    client.headers.update(
        {"Origin": "http://localhost:3100", "X-CSRF-Token": client.cookies.get("devflow_csrf")}
    )
    return client


def _postgres_test_url() -> str | None:
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        return url
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            value = line.split("=", 1)[1].strip()
            base, _, dbname = value.rpartition("/")
            if not base or not dbname:
                return None
            return f"{base}/devflow_test"
    return None


@pytest.fixture(scope="session")
def pg_url():
    """Real PostgreSQL URL for queue/concurrency integration tests.

    Prefers TEST_DATABASE_URL; otherwise derives a devflow_test database from
    the project .env. Skips cleanly when PostgreSQL is unreachable.
    """
    url = _postgres_test_url()
    if not url:
        pytest.skip("no PostgreSQL test URL")
    # Prefer IPv4: on Windows, "localhost" often resolves to ::1 first, which
    # the Docker port mapping does not answer, costing seconds per connection.
    url = url.replace("@localhost:", "@127.0.0.1:")
    import psycopg

    dsn = url.replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            return url
    except psycopg.OperationalError:
        base_dsn = dsn.replace("/devflow_test", "/devflow", 1)
        try:
            with psycopg.connect(base_dsn, connect_timeout=3, autocommit=True) as conn:
                conn.execute("CREATE DATABASE devflow_test")
            return url
        except Exception:
            pytest.skip("PostgreSQL is not reachable")


@pytest.fixture
def pg_factory(pg_url):
    engine = create_engine(pg_url, pool_pre_ping=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    yield factory
    engine.dispose()
