"""Durable workflow, approval and extension records (M5–M7)."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base
from app.persistence.models.base import utcnow


class TaskRun(Base):
    __tablename__ = "task_runs"
    __table_args__ = (UniqueConstraint("run_id", "key", name="uq_task_run_key"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"), index=True)
    child_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("runs.id"))
    key: Mapped[str] = mapped_column(String(80))
    agent: Mapped[str] = mapped_column(String(40))
    state: Mapped[str] = mapped_column(String(20), default="pending")
    plan_version: Mapped[int] = mapped_column(Integer, default=1)
    specification: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkflowCheckpoint(Base):
    __tablename__ = "workflow_checkpoints"
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BudgetReservation(Base):
    __tablename__ = "budget_reservations"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("budget_accounts.id"), index=True)
    tokens: Mapped[int] = mapped_column(Integer)
    actual_tokens: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(20), default="reserved")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MemoryCandidate(Base):
    __tablename__ = "memory_candidates"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    repo_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"))
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    state: Mapped[str] = mapped_column(String(20), default="pending")
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MemoryVersion(Base):
    __tablename__ = "memory_versions"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    candidate_id: Mapped[UUID] = mapped_column(ForeignKey("memory_candidates.id"), unique=True)
    repo_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    state: Mapped[str] = mapped_column(String(20), default="active")
    payload: Mapped[dict] = mapped_column(JSON)
    approved_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConversationSummary(Base):
    __tablename__ = "conversation_summaries"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(ForeignKey("conversations.id"), index=True)
    through_seq: Mapped[int] = mapped_column(Integer)
    message_hash: Mapped[str] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ActionDraft(Base):
    __tablename__ = "action_drafts"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    repo_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(40))
    state: Mapped[str] = mapped_column(String(20), default="pending")
    version: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    preconditions: Mapped[dict] = mapped_column(JSON, default=dict)
    approved_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_hash: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ActionVersion(Base):
    __tablename__ = "action_versions"
    __table_args__ = (UniqueConstraint("draft_id", "version", name="uq_action_version"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    draft_id: Mapped[UUID] = mapped_column(ForeignKey("action_drafts.id"))
    version: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ActionExecution(Base):
    __tablename__ = "action_executions"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    draft_id: Mapped[UUID] = mapped_column(ForeignKey("action_drafts.id"), unique=True)
    job_id: Mapped[UUID | None] = mapped_column(ForeignKey("jobs.id"))
    state: Mapped[str] = mapped_column(String(20), default="queued")
    result: Mapped[dict | None] = mapped_column(JSON)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    repo_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    actor_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    entity_id: Mapped[UUID] = mapped_column(index=True)
    event: Mapped[str] = mapped_column(String(60))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WeeklyReport(Base):
    __tablename__ = "weekly_reports"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    repo_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"), unique=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(String(80))
    content: Mapped[dict] = mapped_column(JSON)
    document_id: Mapped[UUID | None] = mapped_column(ForeignKey("documents.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ExtensionConfig(Base):
    __tablename__ = "extension_configs"
    __table_args__ = (UniqueConstraint("repo_id", "name", "version", name="uq_extension_version"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    repo_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    version: Mapped[int] = mapped_column(Integer, default=1)
    kind: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(20), default="active")
    schema_hash: Mapped[str] = mapped_column(String(64))
    content: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
