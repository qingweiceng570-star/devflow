from sqlalchemy import select

from app.core.errors import DomainError
from app.persistence.models import Probe


def test_fixture_probe_is_persisted_and_not_live(logged_in, database):
    response = logged_in.post(
        "/api/v1/settings/capabilities/probe", json={"source": "fixture", "case_id": "risk_pr"}
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["passed"] and not data["live_model_verified"]
    assert data["provider"] == "fixture" and data["synthetic_input"]
    with database() as db:
        assert db.scalar(select(Probe)).result["answer"]["observed_value"] == "DEVFLOW_RISK_PR_V1"
    assert len(logged_in.get("/api/v1/settings/capabilities/probes").json()["data"]) == 1


def test_real_probe_without_credentials_is_unavailable(logged_in):
    response = logged_in.post("/api/v1/settings/capabilities/probe", json={"source": "configured"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CAPABILITY_UNAVAILABLE"


def test_capabilities_do_not_expose_secrets(logged_in):
    data = logged_in.get("/api/v1/settings/capabilities").json()["data"]
    assert not data["model_configured"]
    assert data["external_writes"] is False
    assert "api_key" not in data


def test_probe_lock_rejects_concurrent_probe(logged_in):
    lock = logged_in.app.state.probe_lock
    lock.acquire()
    try:
        assert logged_in.post("/api/v1/settings/capabilities/probe", json={}).status_code == 409
    finally:
        lock.release()


def test_model_initialization_failure_releases_lock(logged_in):
    original_factory = logged_in.app.state.model_factory

    def unavailable_model(settings, source):
        raise DomainError("CAPABILITY_UNAVAILABLE", "模型不可用", 503)

    logged_in.app.state.model_factory = unavailable_model
    response = logged_in.post("/api/v1/settings/capabilities/probe", json={"source": "fixture"})
    assert response.json()["data"]["passed"] is False
    assert not logged_in.app.state.probe_lock.locked()
    logged_in.app.state.model_factory = original_factory
    assert logged_in.post("/api/v1/settings/capabilities/probe", json={"source": "fixture"}).json()[
        "data"
    ]["passed"]


def test_health_is_available_without_login(client):
    assert client.get("/api/v1/health/live").status_code == 200
    assert (
        client.get("/api/v1/health/ready").status_code == 503
    )  # SQLite fixture has no migration revision.
