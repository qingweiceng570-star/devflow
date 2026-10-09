from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base
from app.persistence.models.base import utcnow

_ACTIVE_RUN_STATUSES = "status IN ('queued', 'running', 'recovering')"


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    repo_id: Mapped[UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), index=True
    )
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200), default="新会话")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "seq", name="uq_messages_conversation_seq"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    repo_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(10))
    content: Mapped[str] = mapped_column(Text)
    run_id: Mapped[UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        Index(
            "uq_runs_active_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text(f"conversation_id IS NOT NULL AND {_ACTIVE_RUN_STATUSES}"),
            sqlite_where=text(f"conversation_id IS NOT NULL AND {_ACTIVE_RUN_STATUSES}"),
        ),
        Index("ix_runs_repo_created", "repo_id", "created_at"),
        Index("ix_runs_repo_status", "repo_id", "status"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    repo_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    target: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON)
    partial_result: Mapped[dict | None] = mapped_column(JSON)
    stop_reason: Mapped[str | None] = mapped_column(String(30))
    error: Mapped[dict | None] = mapped_column(JSON)
    config_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    context_through_seq: Mapped[int | None] = mapped_column(Integer)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parent_run_id: Mapped[UUID | None] = mapped_column(Uuid)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RunEvidence(Base):
    __tablename__ = "run_evidence"
    __table_args__ = (
        UniqueConstraint("run_id", "evidence_id", "usage", name="uq_run_evidence_binding"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    evidence_id: Mapped[UUID] = mapped_column(Uuid)
    usage: Mapped[str] = mapped_column(String(20), default="collected")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[UUID | None] = mapped_column(Uuid)
    agent: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(120))
    input_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="running")
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ToolCall(Base):
    __tablename__ = "tool_calls"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "logical_call_id", "attempt", name="uq_tool_calls_run_logical_attempt"
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    agent_run_id: Mapped[UUID | None] = mapped_column(Uuid)
    logical_call_id: Mapped[UUID] = mapped_column(Uuid)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    tool: Mapped[str] = mapped_column(String(60))
    args: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20))
    result: Mapped[dict | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(40))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_run_events_run_seq"),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BudgetAccount(Base):
    __tablename__ = "budget_accounts"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), unique=True
    )
    limit_tokens: Mapped[int] = mapped_column(Integer)
    consumed_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reserved_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
