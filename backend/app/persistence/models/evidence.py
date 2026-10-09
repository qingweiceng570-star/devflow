from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base
from app.persistence.models.base import utcnow


class SourceSnapshot(Base):
    __tablename__ = "source_snapshots"
    __table_args__ = (
        Index("ix_source_snapshots_repo_type_external", "repo_id", "source_type", "external_id"),
        Index("ix_source_snapshots_repo_hash", "repo_id", "content_hash"),
        Index("ix_source_snapshots_repo_id_id", "repo_id", "id", unique=True),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    repo_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    source_type: Mapped[str] = mapped_column(String(30))
    external_id: Mapped[str] = mapped_column(String(80))
    revision: Mapped[str | None] = mapped_column(String(120))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    object_ref: Mapped[str] = mapped_column(String(300))
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Evidence(Base):
    __tablename__ = "evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["repo_id", "snapshot_id"],
            ["source_snapshots.repo_id", "source_snapshots.id"],
            name="fk_evidence_snapshot",
        ),
        Index("ix_evidence_repo_created", "repo_id", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    repo_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    snapshot_id: Mapped[UUID | None] = mapped_column(Uuid)
    source_type: Mapped[str] = mapped_column(String(30))
    source_url: Mapped[str | None] = mapped_column(String(500))
    locator: Mapped[dict] = mapped_column(JSON, default=dict)
    excerpt: Mapped[str] = mapped_column(String(2000))
    content_hash: Mapped[str] = mapped_column(String(64))
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        Index("uq_idempotency_actor_route_key", "actor_id", "route", "key", unique=True),
        Index("ix_idempotency_expires", "expires_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    route: Mapped[str] = mapped_column(String(120))
    key: Mapped[str] = mapped_column(String(80))
    request_hash: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[UUID | None] = mapped_column(Uuid)
    response: Mapped[dict | None] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
