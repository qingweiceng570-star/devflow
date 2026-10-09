from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base
from app.persistence.models.base import utcnow


class Repository(Base):
    __tablename__ = "repositories"
    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="uq_repositories_provider_external"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    provider: Mapped[str] = mapped_column(String(20), default="github")
    external_id: Mapped[str] = mapped_column(String(64))
    full_name: Mapped[str] = mapped_column(String(300), index=True)
    credential_ref: Mapped[str] = mapped_column(String(80), default="github-default")
    default_branch: Mapped[str | None] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(20), default="active")
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RepoMembership(Base):
    __tablename__ = "repo_memberships"
    __table_args__ = (UniqueConstraint("user_id", "repo_id", name="uq_repo_memberships_user_repo"),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    repo_id: Mapped[UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(20), default="admin")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Issue(Base):
    __tablename__ = "issues"
    __table_args__ = (
        UniqueConstraint("repo_id", "number", name="uq_issues_repo_number"),
        UniqueConstraint("repo_id", "external_id", name="uq_issues_repo_external"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    repo_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    external_id: Mapped[str] = mapped_column(String(64))
    number: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(10))
    title: Mapped[str] = mapped_column(String(1024))
    body_ref: Mapped[str | None] = mapped_column(String(300))
    author_login: Mapped[str | None] = mapped_column(String(120))
    labels: Mapped[list] = mapped_column(JSON, default=list)
    comments_count: Mapped[int] = mapped_column(Integer, default=0)
    html_url: Mapped[str | None] = mapped_column(String(500))
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PullRequest(Base):
    __tablename__ = "pull_requests"
    __table_args__ = (
        UniqueConstraint("repo_id", "number", name="uq_pull_requests_repo_number"),
        UniqueConstraint("repo_id", "external_id", name="uq_pull_requests_repo_external"),
        UniqueConstraint("repo_id", "id", name="uq_pull_requests_repo_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    repo_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    external_id: Mapped[str] = mapped_column(String(64))
    number: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(10))
    merged: Mapped[bool] = mapped_column(Boolean, default=False)
    draft: Mapped[bool] = mapped_column(Boolean, default=False)
    title: Mapped[str] = mapped_column(String(1024))
    body_ref: Mapped[str | None] = mapped_column(String(300))
    author_login: Mapped[str | None] = mapped_column(String(120))
    labels: Mapped[list] = mapped_column(JSON, default=list)
    base_ref: Mapped[str | None] = mapped_column(String(200))
    base_sha: Mapped[str | None] = mapped_column(String(64))
    head_ref: Mapped[str | None] = mapped_column(String(200))
    head_sha: Mapped[str | None] = mapped_column(String(64))
    html_url: Mapped[str | None] = mapped_column(String(500))
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        UniqueConstraint("repo_id", "external_id", name="uq_reviews_repo_external"),
        ForeignKeyConstraint(
            ["repo_id", "pr_id"],
            ["pull_requests.repo_id", "pull_requests.id"],
            name="fk_reviews_pull_request",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    repo_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    pr_id: Mapped[UUID] = mapped_column(Uuid)
    external_id: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(30))
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    author_login: Mapped[str | None] = mapped_column(String(120))
    body_ref: Mapped[str | None] = mapped_column(String(300))
    html_url: Mapped[str | None] = mapped_column(String(500))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CiRun(Base):
    __tablename__ = "ci_runs"
    __table_args__ = (
        UniqueConstraint("repo_id", "external_id", "attempt", name="uq_ci_runs_repo_external_attempt"),
        UniqueConstraint("repo_id", "id", name="uq_ci_runs_repo_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    repo_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    external_id: Mapped[str] = mapped_column(String(64))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    workflow_name: Mapped[str] = mapped_column(String(200))
    head_sha: Mapped[str] = mapped_column(String(64), index=True)
    head_branch: Mapped[str | None] = mapped_column(String(200))
    event: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20))
    conclusion: Mapped[str | None] = mapped_column(String(20))
    html_url: Mapped[str | None] = mapped_column(String(500))
    run_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CiJob(Base):
    __tablename__ = "ci_jobs"
    __table_args__ = (
        UniqueConstraint("repo_id", "external_id", name="uq_ci_jobs_repo_external"),
        ForeignKeyConstraint(
            ["repo_id", "ci_run_id"],
            ["ci_runs.repo_id", "ci_runs.id"],
            name="fk_ci_jobs_ci_run",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    repo_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    ci_run_id: Mapped[UUID] = mapped_column(Uuid)
    external_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20))
    conclusion: Mapped[str | None] = mapped_column(String(20))
    html_url: Mapped[str | None] = mapped_column(String(500))
    logs_ref: Mapped[str | None] = mapped_column(String(300))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ActivityEvent(Base):
    __tablename__ = "activity_events"
    __table_args__ = (
        UniqueConstraint("repo_id", "source_event_id", name="uq_activity_events_repo_source"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    repo_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    source_event_id: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(40))
    actor_login: Mapped[str | None] = mapped_column(String(120))
    subject_type: Mapped[str | None] = mapped_column(String(20))
    subject_number: Mapped[int | None] = mapped_column(Integer)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_ref: Mapped[str | None] = mapped_column(String(300))
    evidence_id: Mapped[UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
