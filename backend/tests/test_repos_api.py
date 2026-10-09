from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from app.integrations.github.client import GitHubClient
from app.persistence.models import Job, Repository, SyncRun
from app.services.evidence_service import capture
from tests.fake_github import FakeGitHub

ORIGIN = {"Origin": "http://localhost:3100"}


@pytest.fixture
def fake():
    return FakeGitHub()


@pytest.fixture
def gh_app(logged_in, fake):
    logged_in.app.state.github_client_factory = lambda settings, token: GitHubClient(
        settings, token, transport=httpx.MockTransport(fake.handler)
    )
    return logged_in


def register(gh_app, full_name="octo/demo"):
    return gh_app.post("/api/v1/repos", json={"full_name": full_name}, headers=ORIGIN)


def test_register_dedupes_by_external_id_and_handles_rename(gh_app, fake):
    response = register(gh_app)
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["created"] is True and data["full_name"] == "octo/demo"
    repo_id = data["id"]

    again = register(gh_app)
    assert again.status_code == 201
    assert again.json()["data"]["created"] is False
    assert again.json()["data"]["id"] == repo_id

    fake.full_name = "octo/renamed"
    renamed = register(gh_app, "octo/renamed")
    body = renamed.json()["data"]
    assert body["id"] == repo_id and body["full_name"] == "octo/renamed"
    assert body["version"] == 2


def test_register_unavailable_repository(gh_app):
    response = register(gh_app, "octo/missing")
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "SOURCE_UNAVAILABLE"


def test_register_validates_format_and_credential(gh_app):
    bad_name = gh_app.post("/api/v1/repos", json={"full_name": "not a repo"}, headers=ORIGIN)
    assert bad_name.status_code == 400
    bad_ref = gh_app.post(
        "/api/v1/repos",
        json={"full_name": "octo/demo", "credential_ref": "unknown-ref"},
        headers=ORIGIN,
    )
    assert bad_ref.status_code == 400
    assert bad_ref.json()["error"]["code"] == "CREDENTIAL_UNKNOWN"


def test_repo_listing_requires_login(client):
    assert client.get("/api/v1/repos", headers=ORIGIN).status_code == 401


def test_repo_get_and_version_conflict(gh_app):
    repo_id = register(gh_app).json()["data"]["id"]
    detail = gh_app.get(f"/api/v1/repos/{repo_id}", headers=ORIGIN).json()["data"]
    assert detail["repo"]["id"] == repo_id
    assert detail["latest_sync"] is None

    conflict = gh_app.patch(
        f"/api/v1/repos/{repo_id}", json={"expected_version": 99, "state": "disabled"}, headers=ORIGIN
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["details"]["current_version"] == 1

    ok = gh_app.patch(
        f"/api/v1/repos/{repo_id}", json={"expected_version": 1, "state": "disabled"}, headers=ORIGIN
    )
    assert ok.status_code == 200 and ok.json()["data"]["state"] == "disabled"


def test_sync_trigger_dedupes_active_run_and_blocks_disabled(gh_app, database):
    repo_id = register(gh_app).json()["data"]["id"]
    first = gh_app.post(f"/api/v1/repos/{repo_id}/sync", json={}, headers=ORIGIN)
    assert first.status_code == 202
    body = first.json()["data"]
    assert body["created"] is True and body["status"] == "queued"
    assert body["resources"] == ["issues", "pulls", "reviews", "ci", "events"]

    second = gh_app.post(f"/api/v1/repos/{repo_id}/sync", json={}, headers=ORIGIN)
    assert second.status_code == 202
    assert second.json()["data"]["created"] is False
    assert second.json()["data"]["id"] == body["id"]

    with database() as db:
        jobs = db.scalars(select(Job)).all()
        assert len(jobs) == 1  # dedupe did not queue a second job

    gh_app.patch(
        f"/api/v1/repos/{repo_id}", json={"expected_version": 1, "state": "disabled"}, headers=ORIGIN
    )
    with database() as db:
        job_row = db.scalar(select(Job))
        job_row.status = "succeeded"
        run = db.get(SyncRun, job_row.entity_id)
        run.status = "succeeded"
        db.commit()
    blocked = gh_app.post(f"/api/v1/repos/{repo_id}/sync", json={}, headers=ORIGIN)
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "REPO_DISABLED"


def test_sync_idempotency_key_replay(gh_app):
    repo_id = register(gh_app).json()["data"]["id"]
    headers = {**ORIGIN, "Idempotency-Key": "key-1"}
    first = gh_app.post(f"/api/v1/repos/{repo_id}/sync", json={"resources": ["issues"]}, headers=headers)
    assert first.status_code == 202
    replay = gh_app.post(
        f"/api/v1/repos/{repo_id}/sync", json={"resources": ["issues"]}, headers=headers
    )
    assert replay.status_code == 202
    assert replay.json() == first.json()

    mismatch = gh_app.post(
        f"/api/v1/repos/{repo_id}/sync", json={"resources": ["pulls"]}, headers=headers
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "IDEMPOTENCY_MISMATCH"


def test_sync_run_and_job_views(gh_app, database):
    repo_id = register(gh_app).json()["data"]["id"]
    sync_id = gh_app.post(f"/api/v1/repos/{repo_id}/sync", json={}, headers=ORIGIN).json()["data"]["id"]

    run = gh_app.get(f"/api/v1/sync-runs/{sync_id}", headers=ORIGIN)
    assert run.status_code == 200
    assert run.json()["data"]["counts"] == {}

    with database() as db:
        job = db.scalar(select(Job))
        job_id = job.id
    view = gh_app.get(f"/api/v1/jobs/{job_id}", headers=ORIGIN)
    assert view.status_code == 200
    data = view.json()["data"]
    assert data["kind"] == "sync" and data["status"] == "queued"
    assert "lease_owner" not in data and "payload" not in data

    missing = gh_app.get("/api/v1/sync-runs/00000000-0000-4000-8000-000000000099", headers=ORIGIN)
    assert missing.status_code == 404


def test_evidence_endpoint_scoped_lookup(gh_app, database, settings):
    repo_id = register(gh_app).json()["data"]["id"]
    with database() as db:
        row = capture(
            db,
            settings,
            repo_id=UUID(repo_id),
            source_type="code",
            content="def handler():\n    return 1\n",
            locator={"commit_sha": "a" * 40, "path": "src/app.py", "line_start": 1, "line_end": 2},
        )
        db.commit()
        evidence_id = row.id

    response = gh_app.get(f"/api/v1/evidence/{evidence_id}", headers=ORIGIN)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["source_type"] == "code"
    assert data["locator"]["path"] == "src/app.py"
    assert "return 1" in data["excerpt"]

    missing = gh_app.get(
        "/api/v1/evidence/00000000-0000-4000-8000-000000000099", headers=ORIGIN
    )
    assert missing.status_code == 404


def test_repo_scoped_access_is_404_for_strangers(gh_app, database):
    # A repository that exists but the caller is not a member of must 404.
    with database() as db:
        foreign = Repository(provider="github", external_id="99", full_name="other/repo")
        db.add(foreign)
        db.commit()
        foreign_id = foreign.id
    response = gh_app.get(f"/api/v1/repos/{foreign_id}", headers=ORIGIN)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
