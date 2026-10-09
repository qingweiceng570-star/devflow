"""Knowledge base service: document upload, deletion and reindex requests.

Every content change writes an index_outbox row and enqueues an index job in
the same transaction; the DB stays authoritative for document state, so a
deleted document is filtered out of retrieval immediately even before the
next generation is built (README 7.4).
"""

import hashlib
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from app.core.config import Settings
from app.core.errors import DomainError
from app.domain.enums import ACTIVE_JOB_STATUSES, JobKind
from app.ingestion.document_parser import parse_document
from app.jobs.queue import enqueue
from app.persistence.models import (
    Chunk,
    Document,
    DocumentVersion,
    IndexGeneration,
    IndexOutbox,
    Job,
    Repository,
)
from app.rag.chunker import chunk_document
from app.storage.local import put_object


def _enqueue_index_job(db: DBSession, repo_id, settings: Settings) -> None:
    active = db.scalar(
        select(Job).where(
            Job.kind == JobKind.INDEX,
            Job.entity_id == repo_id,
            Job.status.in_(ACTIVE_JOB_STATUSES),
        )
    )
    if active is None:
        enqueue(db, kind=JobKind.INDEX, entity_id=repo_id, max_attempts=2)


def _outbox(db: DBSession, repo_id, event_type: str, entity_id, payload: dict | None = None) -> None:
    db.add(
        IndexOutbox(
            repo_id=repo_id, entity_id=entity_id, event_type=event_type, payload=payload or {}
        )
    )


def upload_document(
    db: DBSession,
    settings: Settings,
    repo: Repository,
    *,
    filename: str,
    content: bytes,
    source_revision: str | None = None,
) -> tuple[Document, bool]:
    """Create or refresh one document; returns (document, changed)."""
    parsed = parse_document(filename, content, settings)
    source_key = f"upload:{filename}"
    now = datetime.now(UTC)
    document = db.scalar(
        select(Document).where(Document.repo_id == repo.id, Document.source_key == source_key)
    )
    created = document is None
    if created:
        document = Document(
            repo_id=repo.id,
            source_key=source_key,
            origin="source",
            title=parsed.title,
            byte_size=len(content),
        )
        db.add(document)
        db.flush()

    if document.current_version_id is not None and document.state == "active":
        current = db.get(DocumentVersion, document.current_version_id)
        if (
            current is not None
            and current.content_hash == parsed.content_hash
            and current.parser_version == parsed.parser_version
        ):
            document.updated_at = now
            db.commit()
            return document, False

    version = db.scalar(
        select(DocumentVersion).where(
            DocumentVersion.document_id == document.id,
            DocumentVersion.content_hash == parsed.content_hash,
            DocumentVersion.parser_version == parsed.parser_version,
        )
    )
    if version is None:
        object_ref, _ = put_object(settings.storage_root, repo.id, "documents", content)
        version = DocumentVersion(
            document_id=document.id,
            repo_id=repo.id,
            content_hash=parsed.content_hash,
            source_revision=source_revision,
            parser_version=parsed.parser_version,
            object_ref=object_ref,
        )
        db.add(version)
        db.flush()
        for chunk in chunk_document(parsed.text, settings):
            db.add(
                Chunk(
                    repo_id=repo.id,
                    document_version_id=version.id,
                    ordinal=chunk.ordinal,
                    heading_path=chunk.heading_path,
                    text=chunk.text,
                    content_hash=hashlib.sha256(chunk.text.encode()).hexdigest(),
                    line_start=chunk.line_start,
                    line_end=chunk.line_end,
                    token_estimate=chunk.token_estimate,
                )
            )

    document.current_version_id = version.id
    document.title = parsed.title
    document.byte_size = len(content)
    document.source_revision = source_revision
    document.state = "active"
    document.deleted_at = None
    if not created:
        document.version += 1
    document.updated_at = now
    _outbox(db, repo.id, "document_uploaded", document.id, {"source_key": source_key})
    _enqueue_index_job(db, repo.id, settings)
    db.commit()
    return document, True


def delete_document(db: DBSession, settings: Settings, document: Document, *, expected_version: int) -> Document:
    if document.version != expected_version:
        raise DomainError(
            "VERSION_CONFLICT",
            "文档已更新，请刷新后重试",
            409,
            details={"current_version": document.version},
        )
    if document.state == "deleted":
        return document
    document.state = "deleted"
    document.version += 1
    document.deleted_at = datetime.now(UTC)
    document.updated_at = document.deleted_at
    _outbox(db, document.repo_id, "document_deleted", document.id, {"source_key": document.source_key})
    _enqueue_index_job(db, document.repo_id, settings)
    db.commit()
    return document


def request_reindex(db: DBSession, settings: Settings, repo: Repository) -> None:
    _outbox(db, repo.id, "reindex_requested", None, {})
    _enqueue_index_job(db, repo.id, settings)
    db.commit()


def index_status(db: DBSession, repo_id) -> dict:
    active = db.scalar(
        select(IndexGeneration).where(
            IndexGeneration.repo_id == repo_id, IndexGeneration.status == "active"
        )
    )
    pending = db.scalar(
        select(func.count())
        .select_from(IndexOutbox)
        .where(IndexOutbox.repo_id == repo_id, IndexOutbox.status == "pending")
    )
    building = db.scalar(
        select(func.count())
        .select_from(IndexGeneration)
        .where(IndexGeneration.repo_id == repo_id, IndexGeneration.status == "building")
    )
    latest_failed = db.scalar(
        select(IndexGeneration)
        .where(IndexGeneration.repo_id == repo_id, IndexGeneration.status == "failed")
        .order_by(IndexGeneration.created_at.desc())
        .limit(1)
    )
    return {
        "active_generation": {
            "id": str(active.id),
            "chunk_count": active.chunk_count,
            "embedded": bool((active.embedding_config or {}).get("enabled")),
            "degraded_reason": (active.embedding_config or {}).get("degraded_reason"),
            "activated_at": active.activated_at.isoformat() if active.activated_at else None,
        }
        if active
        else None,
        "pending_changes": pending or 0,
        "building": bool(building),
        "latest_failure": latest_failed.error if latest_failed else None,
    }


def require_document(db: DBSession, repo_id, document_id) -> Document:
    document = db.get(Document, document_id)
    if document is None or document.repo_id != repo_id:
        raise DomainError("RESOURCE_NOT_FOUND", "资源不存在", 404)
    return document
