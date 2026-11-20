from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from app.core.errors import DomainError
from app.integrations.github.client import GitHubClient, SourceRateLimited
from app.persistence.models import (
    CiRun,
    Issue,
    Job,
    PullRequest,
    RepoMembership,
    Repository,
    SyncRun,
    User,
)
from app.services import repo_service
from app.services.evidence_service import capture
from app.services.repo_service import normalize_repo_input
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


def test_register_dedupes_by_external_id_and_handles_rename(gh_app, fake, database):
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
    with database() as db:
        jobs = db.scalars(select(Job).where(Job.kind == "import_docs")).all()
        assert len(jobs) == 1 and str(jobs[0].entity_id) == repo_id


def test_register_unavailable_repository(gh_app):
    response = register(gh_app, "octo/missing")
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "GITHUB_RESOURCE_UNAVAILABLE"


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


@pytest.mark.parametrize(
    "value",
    [
        "https://github.com/octo/demo",
        "https://www.github.com/octo/demo.git?tab=readme#readme",
        "github.com/octo/demo/tree/main",
        "git@github.com:octo/demo.git",
        "ssh://git@github.com/octo/demo.git",
        "ssh://git@github.com:22/octo/demo.git",
        "git://github.com/octo/demo.git",
        "git clone https://github.com/octo/demo.git",
        "git clone git@github.com:octo/demo.git",
        "  'https://github.com/octo/demo'  ",
        "https://github.com/octo/demo。",
        "https://github.com/octo/demo（推荐）",
        "https://github.com//octo//demo",
        "HTTPS://GITHUB.COM/octo/demo",
        "octo/demo",
    ],
)
def test_normalize_repo_input_accepts_common_github_links(value):
    assert normalize_repo_input(value) == "octo/demo"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "请输入 GitHub 仓库链接"),
        ("https://gitlab.com/octo/demo", "仅支持 github.com"),
        ("https://github.com/octo", "请提供完整的 GitHub 仓库链接"),
        ("https://gist.github.com/octo/1234", "仅支持 github.com"),
        ("https://github.com/octo/demo%ZZ", "GitHub 仓库格式不正确"),
    ],
)
def test_normalize_repo_input_rejects_invalid_links(value, message):
    with pytest.raises(DomainError) as caught:
        normalize_repo_input(value)
    assert caught.value.status == 400
    assert message in caught.value.message


def test_register_normalizes_link_pasted_by_user(gh_app):
    response = register(gh_app, "git clone https://github.com/octo/demo.git")
    assert response.status_code == 201
    assert response.json()["data"]["full_name"] == "octo/demo"


def test_register_rejects_non_github_repository_link(gh_app):
    response = register(gh_app, "https://gitlab.com/octo/demo")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


class RateLimitedClient:
    def get(self, _path):
        raise SourceRateLimited(30)


def test_register_falls_back_to_git_when_api_quota_is_exhausted(logged_in, database, monkeypatch):
    logged_in.app.state.github_client_factory = lambda settings, token: RateLimitedClient()
    monkeypatch.setattr(repo_service, "_git_probe", lambda *_: (True, "main"))

    response = logged_in.post(
        "/api/v1/repos", json={"full_name": "octo/fallback"}, headers=ORIGIN
    )
    assert response.status_code == 201
    assert response.json()["data"]["full_name"] == "octo/fallback"
    with database() as db:
        row = db.scalar(select(Repository).where(Repository.full_name == "octo/fallback"))
        assert row is not None
        assert row.external_id == "git:octo/fallback"
        assert row.default_branch == "main"


def test_register_reports_rate_limit_when_git_probe_also_fails(logged_in, monkeypatch):
    logged_in.app.state.github_client_factory = lambda settings, token: RateLimitedClient()
    monkeypatch.setattr(repo_service, "_git_probe", lambda *_: (False, None))

    response = logged_in.post("/api/v1/repos", json={"full_name": "octo/gone"}, headers=ORIGIN)
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "GITHUB_RATE_LIMITED"
    assert "GITHUB_READ_TOKEN" in response.json()["error"]["message"]


def test_register_upgrades_provisional_git_identity(gh_app, database):
    with database() as db:
        row = Repository(
            provider="github", external_id="git:octo/demo", full_name="octo/demo"
        )
        db.add(row)
        db.commit()
        row_id = row.id

    response = register(gh_app)
    assert response.status_code == 201
    body = response.json()["data"]
    assert body["id"] == str(row_id)
    assert body["created"] is False
    with database() as db:
        assert db.get(Repository, row_id).external_id == "12345"


def test_repo_listing_requires_login(client):
    assert client.get("/api/v1/repos", headers=ORIGIN).status_code == 401


def test_repo_workspace_returns_overview_data(gh_app, database):
    repo_id = register(gh_app).json()["data"]["id"]
    now = datetime.now(UTC)
    with database() as db:
        rid = UUID(repo_id)
        db.add(
            Issue(
                repo_id=rid,
                external_id="i1",
                number=1,
                state="open",
                title="Open bug",
                labels=["bug"],
                source_updated_at=now,
            )
        )
        db.add(
            Issue(
                repo_id=rid,
                external_id="i2",
                number=2,
                state="closed",
                title="Won't fix",
                labels=["wontfix"],
                source_updated_at=now,
            )
        )
        db.add(
            PullRequest(
                repo_id=rid,
                external_id="p1",
                number=3,
                state="open",
                title="Open PR",
                source_updated_at=now,
            )
        )
        db.add(
            PullRequest(
                repo_id=rid,
                external_id="p2",
                number=4,
                state="closed",
                merged=True,
                title="Merged PR",
                source_updated_at=now,
            )
        )
        db.add(
            CiRun(
                repo_id=rid,
                external_id="100",
                attempt=1,
                workflow_name="pytest",
                head_sha="a" * 40,
                status="completed",
                conclusion="failure",
                source_created_at=now,
                source_updated_at=now,
            )
        )
        db.commit()

    data = gh_app.get(f"/api/v1/repos/{repo_id}/workspace", headers=ORIGIN).json()["data"]
    assert data["counts"] == {
        "open_issues": 1,
        "open_pulls": 1,
        "closed_issues": 1,
        "rejected_issues": 1,
        "failed_ci": 1,
        "merged_pulls": 1,
    }
    assert data["issues"][0]["number"] == 1 and data["issues"][1]["number"] == 2
    assert data["pulls"][0]["number"] == 3
    assert data["ci"][0]["run_id"] == 100 and data["ci"][0]["conclusion"] == "failure"
    assert data["team"][0]["username"] == "admin"
    assert "knowledge" in data and "latest_sync" in data


def test_repo_list_includes_latest_sync_and_paginates(gh_app, database):
    repo_id = register(gh_app).json()["data"]["id"]
    sync_id = gh_app.post(
        f"/api/v1/repos/{repo_id}/sync", json={}, headers=ORIGIN
    ).json()["data"]["id"]
    with database() as db:
        user = db.scalar(select(User).where(User.username == "admin"))
        extra = Repository(provider="github", external_id="777", full_name="octo/extra")
        db.add(extra)
        db.flush()
        db.add(RepoMembership(user_id=user.id, repo_id=extra.id, role="admin"))
        db.commit()
        extra_id = str(extra.id)

    first = gh_app.get("/api/v1/repos?limit=1", headers=ORIGIN).json()["data"]
    second = gh_app.get(
        f"/api/v1/repos?limit=1&cursor={first['next_cursor']}", headers=ORIGIN
    ).json()["data"]
    assert len(first["items"]) == 1 and first["next_cursor"]
    assert len(second["items"]) == 1 and second["next_cursor"] is None

    by_id = {item["id"]: item for item in first["items"] + second["items"]}
    assert set(by_id) == {repo_id, extra_id}
    assert by_id[repo_id]["latest_sync"]["id"] == sync_id
    assert by_id[repo_id]["latest_sync"]["status"] == "queued"
    assert by_id[extra_id]["latest_sync"] is None


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
        jobs = db.scalars(select(Job).where(Job.kind == "sync")).all()
        assert len(jobs) == 1  # dedupe did not queue a second job

    gh_app.patch(
        f"/api/v1/repos/{repo_id}", json={"expected_version": 1, "state": "disabled"}, headers=ORIGIN
    )
    with database() as db:
        job_row = db.scalar(select(Job).where(Job.kind == "sync"))
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
        job = db.scalar(select(Job).where(Job.kind == "sync"))
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
