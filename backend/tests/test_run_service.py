import pytest
from sqlalchemy import select

from app.core.errors import DomainError
from app.domain.enums import JobKind, MessageRole, RunStatus
from app.persistence.models import (
    BudgetAccount,
    Conversation,
    Job,
    Message,
    Repository,
    Run,
    RunEvent,
    User,
)
from app.services import run_service


def setup(database):
    with database() as db:
        user_id = db.scalar(select(User.id).where(User.username == "admin"))
        repo = Repository(provider="github", external_id="1", full_name="octo/demo")
        db.add(repo)
        db.flush()
        conversation = Conversation(repo_id=repo.id, owner_id=user_id)
        db.add(conversation)
        db.commit()
        return repo.id, conversation.id, user_id


def submit(database, settings, conversation_id, message="请审查 PR"):
    with database() as db:
        conversation = db.get(Conversation, conversation_id)
        repo = db.get(Repository, conversation.repo_id)
        return run_service.submit_run(
            db,
            settings,
            conversation=conversation,
            repo=repo,
            message_text=message,
            kind="pr_review",
            target={"pr_number": 7},
        ).id


def test_submit_is_atomic_and_ordered(database, settings):
    _, conversation_id, user_id = setup(database)
    run_id = submit(database, settings, conversation_id)
    with database() as db:
        run = db.get(Run, run_id)
        assert run.status == RunStatus.QUEUED
        assert run.context_through_seq == 1
        assert run.config_snapshot["prompt_version"] == run_service.PROMPT_VERSION
        message = db.scalar(select(Message).where(Message.run_id == run_id))
        assert message.role == MessageRole.USER and message.seq == 1
        job = db.scalar(select(Job))
        assert job.kind == JobKind.ANALYZE and job.entity_id == run_id
        assert job.max_attempts == 1
        budget = db.scalar(select(BudgetAccount))
        assert budget.limit_tokens == settings.agent_total_token_budget
        event = db.scalar(select(RunEvent))
        assert (event.type, event.seq) == ("run.queued", 1)


def test_second_submission_conflicts_while_active(database, settings):
    _, conversation_id, _ = setup(database)
    run_id = submit(database, settings, conversation_id)
    with pytest.raises(DomainError) as caught:
        submit(database, settings, conversation_id)
    assert caught.value.code == "ACTIVE_RUN_EXISTS"
    assert caught.value.details == {"run_id": str(run_id)}


def test_terminal_run_allows_new_submission(database, settings):
    _, conversation_id, _ = setup(database)
    first = submit(database, settings, conversation_id)
    with database() as db:
        run = db.get(Run, first)
        run.status = RunStatus.COMPLETED
        db.commit()
    second = submit(database, settings, conversation_id)
    assert second != first
    with database() as db:
        messages = db.scalars(select(Message).order_by(Message.seq)).all()
        assert [m.seq for m in messages] == [1, 2]


def test_submit_validation(database, settings):
    _, conversation_id, _ = setup(database)
    with database() as db:
        conversation = db.get(Conversation, conversation_id)
        repo = db.get(Repository, conversation.repo_id)
        with pytest.raises(DomainError):
            run_service.submit_run(
                db, settings, conversation=conversation, repo=repo,
                message_text="x", kind="unknown_kind", target={"pr_number": 1},
            )
        with pytest.raises(DomainError):
            run_service.submit_run(
                db, settings, conversation=conversation, repo=repo,
                message_text="x", kind="pr_review", target={},
            )
        settings.max_message_chars = 10
        with pytest.raises(DomainError) as caught:
            run_service.submit_run(
                db, settings, conversation=conversation, repo=repo,
                message_text="x" * 11, kind="pr_review", target={"pr_number": 1},
            )
        assert caught.value.code == "PAYLOAD_TOO_LARGE"


def test_cancel_and_terminal_guard(database, settings):
    _, conversation_id, _ = setup(database)
    run_id = submit(database, settings, conversation_id)
    with database() as db:
        run = run_service.request_cancel(db, db.get(Run, run_id))
        assert run.cancel_requested_at is not None
    with database() as db:
        run = db.get(Run, run_id)
        run.status = RunStatus.CANCELLED
        db.commit()
        with pytest.raises(DomainError) as caught:
            run_service.request_cancel(db, run)
        assert caught.value.code == "RUN_TERMINAL"


def test_finalize_transition_is_guarded(database, settings):
    _, conversation_id, _ = setup(database)
    run_id = submit(database, settings, conversation_id)
    with database() as db:
        run = db.get(Run, run_id)
        # Cannot finalize a queued run without marking it running first.
        assert not run_service.finalize_run(db, run, status=RunStatus.COMPLETED)
        run_service.mark_running(db, run)
        assert run_service.finalize_run(
            db, run, status=RunStatus.COMPLETED, result={"verdict": "hold"}
        )
        # A second terminal transition must lose.
        assert not run_service.finalize_run(db, run, status=RunStatus.FAILED)
    with database() as db:
        run = db.get(Run, run_id)
        assert run.status == RunStatus.COMPLETED and run.finished_at is not None
        seqs = [row.seq for row in db.scalars(select(RunEvent).order_by(RunEvent.seq))]
        assert seqs == [1, 2, 3]


def test_mark_running_conflict_after_state_change(database, settings):
    _, conversation_id, _ = setup(database)
    run_id = submit(database, settings, conversation_id)
    with database() as db:
        run = db.get(Run, run_id)
        run.status = RunStatus.CANCELLED
        db.commit()
        with pytest.raises(DomainError) as caught:
            run_service.mark_running(db, run)
        assert caught.value.code == "RUN_STATE_CHANGED"


def test_render_assistant_text_contains_sections():
    result = {
        "summary": "总体说明",
        "verdict": "hold",
        "findings": [{"severity": "high", "claim": "问题 A", "location": "a.py:1"}],
        "missing_evidence": [{"item": "CI 状态", "impact": "无法确认"}],
        "next_actions": [{"description": "补充测试"}],
        "limitations": ["范围有限"],
    }
    text = run_service.render_assistant_text(result)
    assert "建议暂缓" in text and "问题 A" in text and "补充测试" in text and "范围有限" in text
