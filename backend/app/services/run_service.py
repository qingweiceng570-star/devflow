"""Business run lifecycle (README 5, 17): atomic submission, event emission,
cancellation request and terminal transitions guarded by conditional updates."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session as DBSession

from app.core.config import Settings
from app.core.errors import DomainError
from app.domain.enums import JobKind, MessageRole, RunKind, RunStatus
from app.jobs.queue import enqueue
from app.persistence.models import BudgetAccount, Conversation, Message, Repository, Run, RunEvent
from app.tools.executor import TOOLSET_VERSION

PROMPT_VERSION = "pr_review.v1"

ACTIVE_RUN_STATUSES = (
    RunStatus.QUEUED,
    RunStatus.RUNNING,
    RunStatus.RECOVERING,
)
TERMINAL_RUN_STATUSES = (
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
    RunStatus.TIMED_OUT,
)

VALID_KINDS = {RunKind.PR_REVIEW.value}


def next_event_seq(db: DBSession, run_id: UUID) -> int:
    current = db.scalar(select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id))
    return (current or 0) + 1


def emit(db: DBSession, run: Run, event_type: str, payload: dict) -> RunEvent:
    """Append one event; callers commit it in the same transaction as the state
    change it describes (README 19.2)."""
    event = RunEvent(
        run_id=run.id,
        seq=next_event_seq(db, run.id),
        type=event_type,
        payload=payload,
    )
    db.add(event)
    db.flush()
    return event


def next_message_seq(db: DBSession, conversation_id: UUID) -> int:
    current = db.scalar(
        select(func.max(Message.seq)).where(Message.conversation_id == conversation_id)
    )
    return (current or 0) + 1


def config_snapshot(settings: Settings) -> dict:
    return {
        "prompt_version": PROMPT_VERSION,
        "toolset_version": TOOLSET_VERSION,
        "model": settings.llm_model,
        "provider": settings.llm_provider,
        "agent_max_steps": settings.agent_max_steps,
        "agent_max_tool_calls": settings.agent_max_tool_calls,
        "agent_total_token_budget": settings.agent_total_token_budget,
        "agent_timeout_seconds": settings.agent_timeout_seconds,
        "model_output_token_limit": settings.model_output_token_limit,
        "max_diff_files": settings.max_diff_files,
        "max_diff_bytes": settings.max_diff_bytes,
    }


def validate_target(kind: str, target: dict) -> dict:
    if kind not in VALID_KINDS:
        raise DomainError("INVALID_REQUEST", f"暂不支持的分析类型: {kind}", 400)
    if kind == RunKind.PR_REVIEW.value:
        pr_number = target.get("pr_number")
        if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number < 1:
            raise DomainError("INVALID_REQUEST", "pr_review 需要有效的 pr_number", 400)
        return {"pr_number": pr_number}
    return {}


def submit_run(
    db: DBSession,
    settings: Settings,
    *,
    conversation: Conversation,
    repo: Repository,
    message_text: str,
    kind: str,
    target: dict,
) -> Run:
    """Create message + run + job + first event atomically (README 3.2 step 2)."""
    if repo.state != "active":
        raise DomainError("REPO_DISABLED", "仓库已禁用，不能发起分析", 409)
    text = message_text.strip()
    if not text:
        raise DomainError("INVALID_REQUEST", "消息不能为空", 400)
    if len(text) > settings.max_message_chars:
        raise DomainError("PAYLOAD_TOO_LARGE", "消息超过长度上限", 413)
    cleaned_target = validate_target(kind, target)

    existing = db.scalar(
        select(Run).where(
            Run.conversation_id == conversation.id,
            Run.status.in_(ACTIVE_RUN_STATUSES),
        )
    )
    if existing is not None:
        raise DomainError(
            "ACTIVE_RUN_EXISTS",
            "该会话已有正在执行的分析，请等待完成或取消",
            409,
            details={"run_id": str(existing.id)},
        )

    now = datetime.now(UTC)
    run = Run(
        repo_id=repo.id,
        conversation_id=conversation.id,
        kind=kind,
        status=RunStatus.QUEUED,
        target=cleaned_target,
        config_snapshot=config_snapshot(settings),
    )
    db.add(run)
    db.flush()
    message = Message(
        conversation_id=conversation.id,
        repo_id=repo.id,
        seq=next_message_seq(db, conversation.id),
        role=MessageRole.USER,
        content=text,
        run_id=run.id,
    )
    db.add(message)
    db.flush()
    run.context_through_seq = message.seq
    db.add(BudgetAccount(run_id=run.id, limit_tokens=settings.agent_total_token_budget))
    enqueue(
        db,
        kind=JobKind.ANALYZE,
        entity_id=run.id,
        max_attempts=1,  # analysis is not auto-retried; the user starts a new run
    )
    conversation.updated_at = now
    emit(db, run, "run.queued", {"kind": kind, "target": cleaned_target})
    db.commit()
    return run


def request_cancel(db: DBSession, run: Run) -> Run:
    if run.status in TERMINAL_RUN_STATUSES:
        raise DomainError("RUN_TERMINAL", "任务已结束，无法取消", 409)
    now = datetime.now(UTC)
    run.cancel_requested_at = run.cancel_requested_at or now
    run.updated_at = now
    db.commit()
    return run


def cancel_requested(db_factory, run_id: UUID) -> bool:
    """Fresh check used between model/tool steps."""
    with db_factory() as db:
        value = db.scalar(select(Run.cancel_requested_at).where(Run.id == run_id))
        return value is not None


def mark_running(db: DBSession, run: Run) -> None:
    now = datetime.now(UTC)
    result = db.execute(
        update(Run)
        .where(Run.id == run.id, Run.status.in_((RunStatus.QUEUED, RunStatus.RECOVERING)))
        .values(status=RunStatus.RUNNING, started_at=run.started_at or now, updated_at=now)
    )
    if result.rowcount != 1:
        db.rollback()
        raise DomainError("RUN_STATE_CHANGED", "任务状态已变化，停止执行", 409)
    run.started_at = run.started_at or now
    run.status = RunStatus.RUNNING
    emit(db, run, "run.started", {})
    db.commit()


def finalize_run(
    db: DBSession,
    run: Run,
    *,
    status: str,
    result: dict | None = None,
    partial_result: dict | None = None,
    stop_reason: str | None = None,
    error: dict | None = None,
    assistant_text: str | None = None,
) -> bool:
    """Conditional terminal transition (README 17.4).

    Returns False when a competing transition (e.g. an earlier cancel) already
    decided the terminal state; the caller must not write further events.
    """
    now = datetime.now(UTC)
    if assistant_text is not None and run.conversation_id is not None:
        db.add(
            Message(
                conversation_id=run.conversation_id,
                repo_id=run.repo_id,
                seq=next_message_seq(db, run.conversation_id),
                role=MessageRole.ASSISTANT,
                content=assistant_text[:20000],
                run_id=run.id,
            )
        )
    values = {
        "status": status,
        "result": result,
        "partial_result": partial_result,
        "stop_reason": stop_reason,
        "error": error,
        "finished_at": now,
        "updated_at": now,
    }
    outcome = db.execute(
        update(Run)
        .where(Run.id == run.id, Run.status.in_((RunStatus.RUNNING, RunStatus.RECOVERING)))
        .values(**values)
    )
    if outcome.rowcount != 1:
        db.rollback()
        return False
    for key, value in values.items():
        setattr(run, key, value)
    event_map = {
        RunStatus.COMPLETED: "run.completed",
        RunStatus.FAILED: "run.failed",
        RunStatus.CANCELLED: "run.cancelled",
        RunStatus.TIMED_OUT: "run.timed_out",
    }
    emit(
        db,
        run,
        event_map[status],
        {
            "status": status,
            "stop_reason": stop_reason,
            "verdict": (result or {}).get("verdict"),
            "error": error,
        },
    )
    db.commit()
    return True


def render_assistant_text(result: dict) -> str:
    """Deterministic display text derived from the validated result."""
    lines = [result.get("summary", "").strip()]
    verdict_names = {
        "recommend_merge": "建议合入（仅本次分析范围内）",
        "hold": "建议暂缓",
        "insufficient_evidence": "证据不足，暂不能判断",
    }
    lines.append(f"\n结论：{verdict_names.get(result.get('verdict'), result.get('verdict'))}")
    findings = result.get("findings") or []
    if findings:
        lines.append("\n发现：")
        for item in findings[:20]:
            lines.append(
                f"- [{item.get('severity')}] {item.get('claim')}（{item.get('location')}）"
            )
    missing = result.get("missing_evidence") or []
    if missing:
        lines.append("\n证据缺口：")
        for item in missing[:10]:
            lines.append(f"- {item.get('item')}：{item.get('impact')}")
    actions = result.get("next_actions") or []
    if actions:
        lines.append("\n建议下一步：")
        for item in actions[:10]:
            lines.append(f"- {item.get('description')}")
    limitations = result.get("limitations") or []
    if limitations:
        lines.append("\n局限性：")
        for item in limitations[:10]:
            lines.append(f"- {item}")
    return "\n".join(lines)[:20000]
