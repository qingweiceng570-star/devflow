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


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("repo_id", "source_key", name="uq_documents_repo_source"),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    repo_id: Mapped[UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), index=True
    )
    source_key: Mapped[str] = mapped_column(String(300))
    origin: Mapped[str] = mapped_column(String(20), default="source")
    title: Mapped[str | None] = mapped_column(String(300))
    state: Mapped[str] = mapped_column(String(20), default="active")
    version: Mapped[int] = mapped_column(Integer, default=1)
    byte_size: Mapped[int] = mapped_column(Integer, default=0)
    source_revision: Mapped[str | None] = mapped_column(String(120))
    # Maintained by the service layer; a plain UUID avoids a circular FK
    # between documents and document_versions.
    current_version_id: Mapped[UUID | None] = mapped_column(Uuid)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "content_hash", "parser_version", name="uq_document_versions_hash"
        ),
        UniqueConstraint("repo_id", "id", name="uq_document_versions_repo_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    repo_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    content_hash: Mapped[str] = mapped_column(String(64))
    source_revision: Mapped[str | None] = mapped_column(String(120))
    parser_version: Mapped[str] = mapped_column(String(30))
    object_ref: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("document_version_id", "ordinal", name="uq_chunks_version_ordinal"),
        Index("ix_chunks_repo_version", "repo_id", "document_version_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    repo_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    document_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE")
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    heading_path: Mapped[list] = mapped_column(JSON, default=list)
    text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    line_start: Mapped[int] = mapped_column(Integer)
    line_end: Mapped[int] = mapped_column(Integer)
    token_estimate: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IndexGeneration(Base):
    __tablename__ = "index_generations"
    __table_args__ = (
        Index(
            "uq_index_generations_active_repo",
            "repo_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    repo_id: Mapped[UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="building")
    embedding_config: Mapped[dict] = mapped_column(JSON, default=dict)
    bm25_manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    source_manifest_hash: Mapped[str | None] = mapped_column(String(64))
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IndexMembership(Base):
    __tablename__ = "index_memberships"
    __table_args__ = (
        UniqueConstraint("generation_id", "chunk_id", name="uq_index_memberships_generation_chunk"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    repo_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    generation_id: Mapped[UUID] = mapped_column(
        ForeignKey("index_generations.id", ondelete="CASCADE"), index=True
    )
    chunk_id: Mapped[UUID] = mapped_column(Uuid, index=True)


class IndexOutbox(Base):
    __tablename__ = "index_outbox"
    __table_args__ = (Index("ix_index_outbox_status_created", "status", "created_at"),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    repo_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    entity_id: Mapped[UUID | None] = mapped_column(Uuid)
    event_type: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    error: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
