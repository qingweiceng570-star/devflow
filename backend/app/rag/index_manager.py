"""Index generation lifecycle (README 7.4, 13.4).

Build: collect all current chunks of active documents -> BM25 artifact ->
optional vector upsert (failure downgrades the generation to BM25-only, never
claims a complete RAG) -> membership rows -> validate counts -> atomically
activate. The previous generation stays queryable; older artifacts are
collected (at most one superseded generation is kept).
"""

import logging
import shutil
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session as DBSession

from app.core.config import Settings
from app.core.errors import DomainError
from app.persistence.models import (
    Chunk,
    Document,
    DocumentVersion,
    IndexGeneration,
    IndexMembership,
    IndexOutbox,
    Repository,
)
from app.rag.bm25_store import Bm25Index
from app.rag.chunker import CHUNKER_VERSION, ESTIMATOR_VERSION, source_manifest
from app.rag.embeddings import EmbeddingClient
from app.rag.retriever import index_dir
from app.rag.tokenizer import TOKENIZER_VERSION
from app.rag.vector_store import MilvusVectorStore, VectorUnavailable

log = logging.getLogger("devflow.index")

KEPT_SUPERSEDED_GENERATIONS = 1


def _current_chunks(db: DBSession, repo_id: UUID) -> list[Chunk]:
    return list(
        db.scalars(
            select(Chunk)
            .join(DocumentVersion, DocumentVersion.id == Chunk.document_version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(Chunk.repo_id == repo_id, Document.state == "active")
            .order_by(Chunk.document_version_id, Chunk.ordinal)
        )
    )


def build_and_activate(
    db: DBSession,
    settings: Settings,
    repo_id: UUID,
    *,
    embedding_factory=None,
    vector_store_factory=None,
) -> IndexGeneration:
    repo = db.get(Repository, repo_id)
    if repo is None:
        raise DomainError("REPO_MISSING", "仓库记录不存在", 404)

    chunks = _current_chunks(db, repo_id)
    if len(chunks) > settings.bm25_max_chunks_per_repo:
        message = (
            f"块数量 {len(chunks)} 超过单仓库上限 {settings.bm25_max_chunks_per_repo}，"
            "请分区或迁移索引后端"
        )
        attempt = IndexGeneration(
            repo_id=repo_id,
            status="failed",
            embedding_config={},
            bm25_manifest={},
            error={"code": "BM25_LIMIT_EXCEEDED", "message": message},
        )
        db.add(attempt)
        db.commit()
        raise DomainError("INDEX_LIMIT_EXCEEDED", message, 409)

    generation = IndexGeneration(
        repo_id=repo_id,
        status="building",
        embedding_config={
            "enabled": settings.vector_configured,
            "model": settings.embedding_model or None,
            "dimension": settings.embedding_dimension,
        },
        bm25_manifest={
            "tokenizer_version": TOKENIZER_VERSION,
            "estimator_version": ESTIMATOR_VERSION,
            "chunker_version": CHUNKER_VERSION,
        },
        source_manifest_hash=source_manifest(chunks),
        chunk_count=len(chunks),
    )
    db.add(generation)
    db.flush()
    target_dir = index_dir(settings, repo_id, generation.id)

    # --- BM25 artifact -----------------------------------------------------
    try:
        bm25 = Bm25Index.build([(str(chunk.id), chunk.text) for chunk in chunks])
        bm25.save(target_dir / "bm25.json")
    except Exception as error:  # noqa: BLE001
        _mark_failed(db, generation, {"code": "BM25_BUILD_FAILED", "message": type(error).__name__})
        raise
    generation.bm25_manifest = {
        **generation.bm25_manifest,
        "chunk_count": bm25.doc_count,
        "avg_length": round(bm25.avg_length, 2),
    }

    # --- optional vector channel ------------------------------------------
    vector_ready = False
    vector_error = None
    if settings.vector_configured:
        try:
            factory = vector_store_factory or (lambda s: MilvusVectorStore(s))
            embed_factory = embedding_factory or (lambda s: EmbeddingClient(s))
            store = factory(settings)
            embedder = embed_factory(settings)
            vectors = embedder.embed([chunk.text for chunk in chunks]) if chunks else []
            rows = [
                {
                    "chunk_id": str(chunk.id),
                    "repo_id": str(repo_id),
                    "generation_id": str(generation.id),
                    "document_version_id": str(chunk.document_version_id),
                    "vector": vector,
                }
                for chunk, vector in zip(chunks, vectors, strict=True)
            ]
            written = store.upsert(rows)
            if written != len(chunks):
                raise VectorUnavailable(f"vector count mismatch: {written} != {len(chunks)}")
            vector_ready = True
        except VectorUnavailable as error:
            vector_error = str(error)
        except DomainError as error:
            vector_error = error.code
        except Exception as error:  # noqa: BLE001
            vector_error = type(error).__name__
    if vector_error:
        log.warning("vector build degraded for repo=%s: %s", repo_id, vector_error)
    generation.embedding_config = {
        **generation.embedding_config,
        "enabled": vector_ready,
        "degraded_reason": None if vector_ready else (vector_error or "not_configured"),
    }

    # --- memberships and activation ---------------------------------------
    for chunk in chunks:
        db.add(IndexMembership(generation_id=generation.id, chunk_id=chunk.id, repo_id=repo_id))
    db.flush()
    membership_count = db.scalar(
        select(func.count())
        .select_from(IndexMembership)
        .where(IndexMembership.generation_id == generation.id)
    )
    if membership_count != len(chunks):
        _mark_failed(
            db,
            generation,
            {"code": "MEMBERSHIP_MISMATCH", "message": f"{membership_count} != {len(chunks)}"},
        )
        raise DomainError("INDEX_BUILD_FAILED", "索引成员校验失败", 500)

    now = datetime.now(UTC)
    # Order matters for the partial unique index: demote the old active row
    # first, then activate the new generation, in explicit statements.
    db.execute(
        update(IndexGeneration)
        .where(IndexGeneration.repo_id == repo_id, IndexGeneration.status == "active")
        .values(status="superseded", updated_at=now)
    )
    db.execute(
        update(IndexGeneration)
        .where(IndexGeneration.id == generation.id)
        .values(status="active", activated_at=now, updated_at=now)
    )
    db.commit()
    db.refresh(generation)

    _collect_old_generations(db, settings, repo_id, vector_store_factory)
    return generation


def _mark_failed(db: DBSession, generation: IndexGeneration, error: dict) -> None:
    generation.status = "failed"
    generation.error = error
    generation.updated_at = datetime.now(UTC)
    db.commit()


def _collect_old_generations(
    db: DBSession, settings: Settings, repo_id: UUID, vector_store_factory=None
) -> None:
    """Keep the active generation plus one superseded one; drop the rest."""
    generations = list(
        db.scalars(
            select(IndexGeneration)
            .where(IndexGeneration.repo_id == repo_id)
            .order_by(IndexGeneration.created_at.desc())
        )
    )
    kept_superseded = 0
    for generation in generations:
        if generation.status == "active":
            continue
        if generation.status == "superseded" and kept_superseded < KEPT_SUPERSEDED_GENERATIONS:
            kept_superseded += 1
            continue
        shutil.rmtree(index_dir(settings, repo_id, generation.id), ignore_errors=True)
        db.execute(delete(IndexMembership).where(IndexMembership.generation_id == generation.id))
        db.delete(generation)
        generator = generation
        if generator.embedding_config and generator.embedding_config.get("enabled"):
            try:
                factory = vector_store_factory or (lambda s: MilvusVectorStore(s))
                factory(settings).delete_generation(str(generator.id))
            except Exception as error:  # noqa: BLE001 - best-effort vector cleanup
                log.warning("vector cleanup skipped for %s: %s", generator.id, type(error).__name__)
    db.commit()


def pending_outbox(db: DBSession, repo_id: UUID) -> list[IndexOutbox]:
    return list(
        db.scalars(
            select(IndexOutbox)
            .where(IndexOutbox.repo_id == repo_id, IndexOutbox.status == "pending")
            .order_by(IndexOutbox.created_at)
        )
    )
