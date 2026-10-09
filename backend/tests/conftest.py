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
def settings():
    return Settings(
        _env_file=None, app_env="test", database_url="sqlite://", llm_provider="openai_compatible"
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
