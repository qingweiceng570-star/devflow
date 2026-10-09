import json
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from app.domain.enums import MessageRole, RunStatus
from app.integrations.github.client import GitHubClient
from app.persistence.models import Conversation, Job, Message, Run, RunEvent
from app.services import run_service
from tests.fake_github import FakeGitHub

ORIGIN = {"Origin": "http://localhost:3100"}


@pytest.fixture
def gh_app(logged_in):
    fake = FakeGitHub()
    logged_in.app.state.github_client_factory = lambda settings, token: GitHubClient(
        settings, token, transport=httpx.MockTransport(fake.handler)
    )
    return logged_in


def make_repo_and_conversation(gh_app) -> tuple[str, str]:
    repo_id = gh_app.post(
        "/api/v1/repos", json={"full_name": "octo/demo"}, headers=ORIGIN
    ).json()["data"]["id"]
    conversation_id = gh_app.post(
        "/api/v1/conversations", json={"repo_id": repo_id}, headers=ORIGIN
    ).json()["data"]["id"]
    return repo_id, conversation_id


def submit_run(gh_app, conversation_id, **overrides):
    body = {"message": "请审查这个 PR 的风险", "kind": "pr_review", "target": {"pr_number": 7}}
    body.update(overrides)
    return gh_app.post(
        f"/api/v1/conversations/{conversation_id}/runs", json=body, headers=ORIGIN
    )


def test_conversation_and_run_lifecycle(gh_app, database):
    repo_id, conversation_id = make_repo_and_conversation(gh_app)

    response = submit_run(gh_app, conversation_id)
    assert response.status_code == 202
    data = response.json()["data"]
    assert data["status"] == "queued" and data["target"] == {"pr_number": 7}
    assert data["events_url"].endswith(f"/runs/{data['id']}/events")
    run_id = UUID(data["id"])

    with database() as db:
        run = db.get(Run, run_id)
        assert run is not None and run.conversation_id is not None
        assert db.scalar(select(Job)) is not None
        assert db.scalar(select(Message)).role == "user"
        assert db.scalar(select(RunEvent)).type == "run.queued"

    dup = submit_run(gh_app, conversation_id)
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "ACTIVE_RUN_EXISTS"
    assert dup.json()["error"]["details"]["run_id"] == str(run_id)

    detail = gh_app.get(f"/api/v1/runs/{run_id}", headers=ORIGIN).json()["data"]
    assert detail["id"] == str(run_id) and detail["result"] is None

    tools = gh_app.get(f"/api/v1/runs/{run_id}/tools", headers=ORIGIN).json()["data"]
    assert tools == []

    listing = gh_app.get(f"/api/v1/repos/{repo_id}/runs", headers=ORIGIN).json()["data"]
    assert [item["id"] for item in listing["items"]] == [str(run_id)]

    cancel = gh_app.post(f"/api/v1/runs/{run_id}/cancel", headers=ORIGIN)
    assert cancel.status_code == 202
    assert cancel.json()["data"]["cancel_requested_at"] is not None

    conversation = gh_app.get(
        f"/api/v1/conversations/{conversation_id}", headers=ORIGIN
    ).json()["data"]
    assert conversation["active_run"]["id"] == str(run_id)
    assert conversation["repo"]["id"] == repo_id


def test_run_submission_validates_target(gh_app):
    _, conversation_id = make_repo_and_conversation(gh_app)
    missing = gh_app.post(
        f"/api/v1/conversations/{conversation_id}/runs",
        json={"message": "test", "kind": "pr_review"},
        headers=ORIGIN,
    )
    assert missing.status_code == 422
    bad = submit_run(gh_app, conversation_id, target={"pr_number": 0})
    assert bad.status_code == 422


def test_run_idempotency_key_replay(gh_app):
    _, conversation_id = make_repo_and_conversation(gh_app)
    headers = {**ORIGIN, "Idempotency-Key": "run-key-1"}
    body = {"message": "请审查", "kind": "pr_review", "target": {"pr_number": 7}}
    first = gh_app.post(
        f"/api/v1/conversations/{conversation_id}/runs", json=body, headers=headers
    )
    assert first.status_code == 202
    replay = gh_app.post(
        f"/api/v1/conversations/{conversation_id}/runs", json=body, headers=headers
    )
    assert replay.status_code == 202
    assert replay.json() == first.json()

    mismatch = gh_app.post(
        f"/api/v1/conversations/{conversation_id}/runs",
        json={**body, "message": "不同的内容"},
        headers=headers,
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "IDEMPOTENCY_MISMATCH"


def test_messages_listing_with_cursor(gh_app):
    _, conversation_id = make_repo_and_conversation(gh_app)
    run_id = UUID(submit_run(gh_app, conversation_id).json()["data"]["id"])
    with gh_app.app.state.session_factory() as db:
        run = db.get(Run, run_id)
        run.status = RunStatus.COMPLETED
        db.add(
            Message(
                conversation_id=run.conversation_id,
                repo_id=run.repo_id,
                seq=2,
                role=MessageRole.ASSISTANT,
                content="审查完成",
                run_id=run.id,
            )
        )
        db.commit()
    page = gh_app.get(
        f"/api/v1/conversations/{conversation_id}/messages?limit=1", headers=ORIGIN
    ).json()["data"]
    assert len(page["items"]) == 1 and page["next_cursor"] == 1
    page2 = gh_app.get(
        f"/api/v1/conversations/{conversation_id}/messages?after=1", headers=ORIGIN
    ).json()["data"]
    assert [item["seq"] for item in page2["items"]] == [2]
    assert page2["items"][0]["role"] == "assistant"


def test_run_scoped_404_for_foreign_conversation(gh_app, database):
    _, conversation_id = make_repo_and_conversation(gh_app)
    with database() as db:
        repo_id = db.scalar(
            select(Conversation.repo_id).where(Conversation.id == UUID(conversation_id))
        )
        foreign = Conversation(repo_id=repo_id, owner_id=uuid4())
        db.add(foreign)
        db.commit()
        foreign_id = foreign.id
    response = gh_app.get(f"/api/v1/conversations/{foreign_id}", headers=ORIGIN)
    assert response.status_code == 404


def test_sse_streams_to_terminal_and_supports_resume(gh_app, database):
    _, conversation_id = make_repo_and_conversation(gh_app)
    run_id = UUID(submit_run(gh_app, conversation_id).json()["data"]["id"])
    with database() as db:
        run = db.get(Run, run_id)
        run_service.mark_running(db, run)
        run_service.finalize_run(
            db, run, status=RunStatus.COMPLETED, result={"verdict": "hold"}
        )
    with gh_app.stream("GET", f"/api/v1/runs/{run_id}/events", headers=ORIGIN) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = []
        current = {}
        for line in response.iter_lines():
            if line.startswith("id: "):
                current["id"] = int(line[4:])
            elif line.startswith("event: "):
                current["type"] = line[7:]
            elif line.startswith("data: "):
                current["data"] = json.loads(line[6:])
            elif line == "" and current:
                events.append(current)
                current = {}
        types = [event["type"] for event in events]
        assert types == ["run.queued", "run.started", "run.completed"]
        assert events[-1]["data"]["payload"]["verdict"] == "hold"

    # Resume from a cursor: only later events are replayed.
    with gh_app.stream(
        "GET", f"/api/v1/runs/{run_id}/events?after=1", headers=ORIGIN
    ) as response:
        lines = [line for line in response.iter_lines() if line.startswith("event: ")]
        assert [line[7:] for line in lines] == ["run.started", "run.completed"]

    bad = gh_app.get(f"/api/v1/runs/{run_id}/events?after=-1", headers=ORIGIN)
    assert bad.status_code == 400
