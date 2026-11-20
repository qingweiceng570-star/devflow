from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select

from app.persistence.models import (
    Conversation,
    RepoMembership,
    Repository,
    Run,
    RunEvidence,
    User,
)


def seed(database, role="admin"):
    with database() as db:
        user = db.scalar(select(User).where(User.username == "admin"))
        repo = Repository(full_name="test/platform", external_id="platform")
        db.add(repo)
        db.flush()
        db.add(RepoMembership(repo_id=repo.id, user_id=user.id, role=role))
        conv = Conversation(repo_id=repo.id, owner_id=user.id)
        db.add(conv)
        db.flush()
        run = Run(repo_id=repo.id, conversation_id=conv.id, kind="knowledge_qa", status="completed")
        db.add(run)
        db.flush()
        evidence_id = uuid4()
        db.add(RunEvidence(run_id=run.id, evidence_id=evidence_id, usage="collected"))
        db.commit()
        return repo.id, run.id, evidence_id


def test_memory_approval_lifecycle_and_scope(logged_in, database):
    repo, run, evidence = seed(database)
    path = f"/api/v1/repos/{repo}"
    response = logged_in.post(
        path + "/memories/candidates",
        json={
            "run_id": str(run),
            "key": "fixture",
            "content": "Test evidence",
            "evidence_ids": [str(evidence)],
            "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
    )
    assert response.status_code == 201, response.text
    candidate = response.json()["data"]["id"]
    assert (
        logged_in.post(
            path + f"/memories/candidates/{candidate}/decision", json={"decision": "approved"}
        ).status_code
        == 200
    )
    data = logged_in.get(path + "/collaboration").json()["data"]
    assert len(data["memories"]) == 1 and len(data["audit"]) == 2
    memory = data["memories"][0]["id"]
    assert logged_in.post(path + f"/memories/{memory}/revoke", json={}).status_code == 200
    assert logged_in.get(f"/api/v1/repos/{uuid4()}/collaboration").status_code == 404


def test_viewer_cannot_mutate_and_patch_requires_csrf(logged_in, database):
    repo, _, _ = seed(database, "viewer")
    assert logged_in.post(f"/api/v1/repos/{repo}/sync", json={}).status_code == 403
    assert logged_in.post("/api/v1/conversations", json={"repo_id": str(repo)}).status_code == 403
    assert (
        logged_in.patch(
            f"/api/v1/repos/{repo}", json={"expected_version": 1, "state": "disabled"}
        ).status_code
        == 403
    )
    with database() as db:
        membership = db.scalar(select(RepoMembership).where(RepoMembership.repo_id == repo))
        membership.role = "admin"
        db.commit()
    logged_in.headers.pop("X-CSRF-Token")
    assert (
        logged_in.patch(
            f"/api/v1/repos/{repo}", json={"expected_version": 1, "state": "disabled"}
        ).status_code
        == 403
    )


def test_skill_install_version_and_disable(logged_in, database):
    repo, _, _ = seed(database)
    body = {
        "name": "fixture",
        "version": 1,
        "kind": "skill",
        "content": {
            "description": "Fixture transform",
            "input_schema": {"type": "object"},
            "allowed_tools": [],
            "steps": [{"id": "out", "kind": "transform", "arguments": {"note": "fixture"}}],
            "output_schema": {"type": "object"},
            "max_steps": 1,
        },
    }
    path = f"/api/v1/repos/{repo}/extensions"
    response = logged_in.post(path, json=body)
    assert response.status_code == 201, response.text
    extension = response.json()["data"]["id"]
    assert logged_in.post(path, json=body).status_code == 409
    assert logged_in.post(path + f"/{extension}/disable", json={}).status_code == 200
    assert logged_in.get(path).json()["data"][0]["state"] == "disabled"
