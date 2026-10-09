from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base
from app.persistence.models.base import utcnow


class SyncRun(Base):
    __tablename__ = "sync_runs"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    repo_id: Mapped[UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), index=True
    )
    resources: Mapped[list] = mapped_column(JSON)
    mode: Mapped[str] = mapped_column(String(20), default="incremental")
    status: Mapped[str] = mapped_column(String(20), default="queued")
    counts: Mapped[dict] = mapped_column(JSON, default=dict)
    errors: Mapped[list] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SyncCursor(Base):
    __tablename__ = "sync_cursors"
    __table_args__ = (UniqueConstraint("repo_id", "resource", name="uq_sync_cursors_repo_resource"),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    repo_id: Mapped[UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), index=True
    )
    resource: Mapped[str] = mapped_column(String(30))
    committed_watermark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    etag: Mapped[str | None] = mapped_column(String(200))
    coverage: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
