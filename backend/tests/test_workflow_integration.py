import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.jobs.queue import claim_next
from app.persistence.models import (
    BudgetAccount,
    Conversation,
    RepoMembership,
    Repository,
    Run,
    TaskRun,
    User,
)
from app.services import run_service
from app.workflows.release import execute_release


@pytest.mark.parametrize("ci_conclusion,expected_tasks", [("success", 2), ("failure", 3)])
def test_parallel_workflow_checkpoint_resume_and_one_observer_round(
    pg_factory, settings, monkeypatch, ci_conclusion, expected_tasks
):
    with pg_factory() as db:
        user = User(username="admin", password_hash="unused", role="admin")
        repo = Repository(
            full_name="test/repo", external_id="123", config_json={"required_checks": ["test"]}
        )
        db.add_all([user, repo])
        db.flush()
        conv = Conversation(repo_id=repo.id, owner_id=user.id)
        db.add(conv)
        db.flush()
        run = run_service.submit_run(
            db,
            settings,
            conversation=conv,
            repo=repo,
            kind="release_check",
            target={"pr_number": 7, "ci_run_id": 99, "attempt": 2},
            message_text="check release",
        )
        run_id = run.id
        job = claim_next(db, worker_id="test", lease_seconds=600)
        db.commit()
    lock = threading.Lock()
    calls, concurrency, peak = [], 0, 0

    def fake_child(factory, settings, job, worker_id, **kwargs):
        nonlocal concurrency, peak
        with lock:
            concurrency += 1
            peak = max(peak, concurrency)
            calls.append(job.entity_id)
        try:
            with factory() as db:
                run = db.get(Run, job.entity_id)
                run_service.mark_running(db, run)
                assert run.target["expected_head_sha"] == "a" * 40
                time.sleep(0.1)
                result = {
                    "kind": run.kind,
                    "summary": "verified",
                    "coverage": "complete",
                    "freshness": "fresh",
                    "verdict": "recommend_merge" if run.kind == "pr_review" else "not_applicable",
                    "details": {"conclusion": ci_conclusion, "status": "completed"},
                    "findings": [],
                    "hypotheses": [],
                    "limitations": [],
                }
                run_service.finalize_run(db, run, status="completed", result=result)
        finally:
            with lock:
                concurrency -= 1

    monkeypatch.setattr("app.jobs.handlers.analyze.handle_analyze", fake_child)
    client = SimpleNamespace(
        get=lambda *args, **kwargs: SimpleNamespace(data={"head": {"sha": "a" * 40}})
    )
    result = execute_release(pg_factory, settings, job, "test", client_factory=lambda *_: client)
    assert result["status"] == "completed"
    assert peak == 2 and len(calls) == expected_tasks
    with pg_factory() as db:
        run = db.get(Run, run_id)
        assert run.result["verdict"] == (
            "recommend_release" if ci_conclusion == "success" else "hold"
        )
        assert len(db.scalars(select(TaskRun)).all()) == expected_tasks
        assert len(db.scalars(select(BudgetAccount)).all()) == 1
        run.status = "recovering"
        db.commit()
    # Re-entering orchestration only reads committed child results, never calls again.
    execute_release(pg_factory, settings, job, "test", client_factory=lambda *_: client)
    assert len(calls) == expected_tasks


def test_action_worker_unknown_is_not_replayed(database, settings):
    from pydantic import SecretStr

    from app.persistence.models import ActionDraft, ActionExecution
    from app.services import action_service as actions
    from tests.test_platform import ActionRead, draft

    repo_id, actor, draft_id = draft(database, settings)
    settings.enabled_actions = ["create_issue_comment"]
    settings.github_write_token = SecretStr("fake")
    with database() as db:
        repo, row = db.get(Repository, repo_id), db.get(ActionDraft, draft_id)
        db.add(RepoMembership(repo_id=repo_id, user_id=actor, role="maintainer"))
        settings.write_repo_allowlist = [repo.full_name]
        actions.approve(db, row, actor, row.version, row.payload_hash)
        execution = actions.execute(db, settings, repo, row, actor)
        execution_id = execution.id
        job = claim_next(db, worker_id="test", lease_seconds=60)
        db.commit()
    calls = []

    def send(*_):
        calls.append(True)
        return "unknown", {"code": "WRITE_OUTCOME_UNKNOWN"}

    for _ in range(2):
        actions.handle_action(
            database, settings, job, "test", client_factory=lambda *_: ActionRead(), sender=send
        )
    assert calls == [True]
    with database() as db:
        assert db.get(ActionExecution, execution_id).state == "unknown"


def test_action_worker_changed_precondition_never_sends(database, settings):
    from pydantic import SecretStr

    from app.persistence.models import ActionDraft
    from app.services import action_service as actions
    from tests.test_platform import ActionRead, draft

    repo_id, actor, draft_id = draft(database, settings)
    settings.enabled_actions = ["create_issue_comment"]
    settings.github_write_token = SecretStr("fake")
    with database() as db:
        repo, row = db.get(Repository, repo_id), db.get(ActionDraft, draft_id)
        db.add(RepoMembership(repo_id=repo_id, user_id=actor, role="maintainer"))
        settings.write_repo_allowlist = [repo.full_name]
        actions.approve(db, row, actor, row.version, row.payload_hash)
        actions.execute(db, settings, repo, row, actor)
        job = claim_next(db, worker_id="test", lease_seconds=60)
        db.commit()
    calls = []
    result = actions.handle_action(
        database,
        settings,
        job,
        "test",
        client_factory=lambda *_: ActionRead(changed=True),
        sender=lambda *_: calls.append(True),
    )
    assert result["status"] == "stale" and not calls


def test_action_worker_revoked_membership_never_sends(database, settings):
    from pydantic import SecretStr

    from app.persistence.models import ActionDraft, ActionExecution
    from app.services import action_service as actions
    from tests.test_platform import ActionRead, draft

    repo_id, actor, draft_id = draft(database, settings)
    settings.enabled_actions = ["create_issue_comment"]
    settings.github_write_token = SecretStr("fake")
    with database() as db:
        repo, row = db.get(Repository, repo_id), db.get(ActionDraft, draft_id)
        settings.write_repo_allowlist = [repo.full_name]
        actions.approve(db, row, actor, row.version, row.payload_hash)
        execution = actions.execute(db, settings, repo, row, actor)
        execution_id = execution.id
        job = claim_next(db, worker_id="test", lease_seconds=60)
        db.commit()
    sent = []
    actions.handle_action(
        database,
        settings,
        job,
        "test",
        client_factory=lambda *_: ActionRead(),
        sender=lambda *_: sent.append(True),
    )
    assert sent == []
    with database() as db:
        assert db.get(ActionExecution, execution_id).result["code"] == "FORBIDDEN"
