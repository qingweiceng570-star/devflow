from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import func, select

from app.api.deps import current_identity, get_db, mutation_identity, repo_scope
from app.core.errors import DomainError
from app.domain.enums import JobKind
from app.persistence.models import Chunk, Document, DocumentVersion, Job
from app.schemas.requests import DeleteDocumentRequest
from app.schemas.responses import (
    decode_cursor,
    document_version_view,
    document_view,
    encode_cursor,
    envelope,
    page_limit,
)
from app.services import knowledge_service
from app.services.repo_service import require_repo

router = APIRouter()


@router.post("/repos/{repo_id}/documents", status_code=202)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    source_revision: str | None = Form(default=None, max_length=120),
    repo_scope_pair=Depends(repo_scope),
    identity=Depends(mutation_identity),
    db=Depends(get_db),
):
    repo, _ = repo_scope_pair
    if repo.state != "active":
        raise DomainError("REPO_DISABLED", "仓库已禁用，不能上传文档", 409)
    content = await file.read()
    document, changed = knowledge_service.upload_document(
        db,
        request.app.state.settings,
        repo,
        filename=file.filename or "untitled.md",
        content=content,
        source_revision=source_revision,
    )
    job = db.scalar(
        select(Job)
        .where(Job.kind == JobKind.INDEX, Job.entity_id == repo.id)
        .order_by(Job.created_at.desc())
        .limit(1)
    )
    return envelope(
        {
            "document": document_view(document),
            "changed": changed,
            "index_job_id": str(job.id) if job else None,
        }
    )


@router.get("/repos/{repo_id}/documents")
def list_documents(
    repo_scope_pair=Depends(repo_scope),
    limit: int | None = None,
    cursor: str | None = None,
    db=Depends(get_db),
):
    repo, _ = repo_scope_pair
    query = (
        select(Document)
        .where(Document.repo_id == repo.id)
        .order_by(Document.created_at.desc(), Document.id.desc())
    )
    decoded = decode_cursor(cursor)
    if decoded is not None:
        created_at, row_id = decoded
        query = query.where(
            (Document.created_at < created_at)
            | ((Document.created_at == created_at) & (Document.id < row_id))
        )
    rows = db.scalars(query.limit(page_limit(limit) + 1)).all()
    has_more = len(rows) > page_limit(limit)
    rows = rows[: page_limit(limit)]
    next_cursor = encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None
    return envelope({"items": [document_view(row) for row in rows], "next_cursor": next_cursor})


@router.get("/documents/{document_id}")
def get_document(
    document_id: UUID,
    identity=Depends(current_identity),
    db=Depends(get_db),
):
    """Reads a specific (current) version's metadata; historical versions are
    listed but never silently substituted (README 13.4)."""
    user, _ = identity
    document = db.get(Document, document_id)
    if document is None:
        raise DomainError("RESOURCE_NOT_FOUND", "资源不存在", 404)
    require_repo(db, user, document.repo_id)
    versions = db.scalars(
        select(DocumentVersion)
        .where(DocumentVersion.document_id == document.id)
        .order_by(DocumentVersion.created_at.desc())
    ).all()
    chunk_count = 0
    if document.current_version_id:
        chunk_count = db.scalar(
            select(func.count())
            .select_from(Chunk)
            .where(Chunk.document_version_id == document.current_version_id)
        )
    return envelope(
        {
            "document": document_view(document),
            "versions": [document_version_view(version) for version in versions],
            "current_chunk_count": chunk_count or 0,
        }
    )


@router.delete("/documents/{document_id}", status_code=202)
def delete_document(
    document_id: UUID,
    body: DeleteDocumentRequest,
    request: Request,
    identity=Depends(mutation_identity),
    db=Depends(get_db),
):
    user, _ = identity
    document = db.get(Document, document_id)
    if document is None:
        raise DomainError("RESOURCE_NOT_FOUND", "资源不存在", 404)
    require_repo(db, user, document.repo_id)
    document = knowledge_service.delete_document(
        db, request.app.state.settings, document, expected_version=body.expected_version
    )
    return envelope({"document": document_view(document), "tombstone": True})
