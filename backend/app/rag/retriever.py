"""Hybrid retrieval: BM25 + vector channels, RRF fusion, DB-authoritative
filtering and bounded packing (README 13.3, 13.4).

The vector store never decides access or freshness: every candidate goes back
through PostgreSQL (repo scope, document state, generation membership) before
it can appear in a result. Missing channels are surfaced as degraded flags;
both channels failing raises RETRIEVAL_UNAVAILABLE.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.core.config import Settings
from app.core.errors import DomainError
from app.persistence.models import (
    Chunk,
    Document,
    DocumentVersion,
    IndexGeneration,
    IndexMembership,
)
from app.rag.bm25_store import Bm25Index
from app.rag.chunker import ESTIMATOR_VERSION
from app.rag.embeddings import EmbeddingClient
from app.rag.tokenizer import TOKENIZER_VERSION
from app.rag.vector_store import MilvusVectorStore, VectorUnavailable

log = logging.getLogger("devflow.retrieval")

RRF_K = 60


@dataclass
class RetrievedChunk:
    chunk_id: UUID
    document_id: UUID
    document_version_id: UUID
    source_key: str
    title: str | None
    ordinal: int
    heading_path: list
    text: str
    line_start: int
    line_end: int
    scores: dict = field(default_factory=dict)


@dataclass
class RetrievalTrace:
    query: str
    generation_id: str | None = None
    channels: dict = field(default_factory=dict)
    fused: int = 0
    selected: list = field(default_factory=list)
    dropped_by_filtering: list = field(default_factory=list)
    degraded: list = field(default_factory=list)
    reranked: bool = False
    tokenizer_version: str = TOKENIZER_VERSION
    estimator_version: str = ESTIMATOR_VERSION

    def as_dict(self) -> dict:
        return {
            "query": self.query,
            "generation_id": self.generation_id,
            "channels": self.channels,
            "fused_candidates": self.fused,
            "selected": self.selected,
            "dropped_by_filtering": self.dropped_by_filtering,
            "degraded": self.degraded,
            "reranked": self.reranked,
            "tokenizer_version": self.tokenizer_version,
            "estimator_version": self.estimator_version,
        }


def index_dir(settings: Settings, repo_id: UUID, generation_id: UUID):
    return settings.storage_root / "indexes" / str(repo_id) / str(generation_id)


def _rrf_fuse(channel_rankings: dict[str, list[str]]) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranking in channel_rankings.values():
        for rank, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
    return scores


def _load_bm25(settings: Settings, repo_id: UUID, generation: IndexGeneration) -> Bm25Index:
    return Bm25Index.load(index_dir(settings, repo_id, generation.id) / "bm25.json")


def retrieve(
    db: DBSession,
    settings: Settings,
    *,
    repo_id: UUID,
    query: str,
    source_types: list[str] | None = None,
    date_range: tuple[datetime | None, datetime | None] | None = None,
    final_k: int | None = None,
    bm25_loader=None,
    vector_store_factory=None,
    embedding_factory=None,
) -> tuple[list[RetrievedChunk], RetrievalTrace]:
    trace = RetrievalTrace(query=query)
    generation = db.scalar(
        select(IndexGeneration).where(
            IndexGeneration.repo_id == repo_id, IndexGeneration.status == "active"
        )
    )
    if generation is None:
        trace.degraded.append("no_index")
        return [], trace
    trace.generation_id = str(generation.id)
    top_k = settings.rag_top_k_per_channel

    # --- BM25 channel ------------------------------------------------------
    bm25_hits = []
    loader = bm25_loader or (lambda repo, gen: _load_bm25(settings, repo, gen))
    try:
        bm25_index = loader(repo_id, generation)
        bm25_hits = bm25_index.search(query, top_k)
        trace.channels["bm25"] = {
            "enabled": True,
            "count": len(bm25_hits),
            "top": [
                {"chunk_id": hit.chunk_id, "score": hit.score} for hit in bm25_hits[:5]
            ],
        }
    except Exception as error:  # noqa: BLE001 - any channel failure degrades to single-path
        log.warning("bm25 channel failed: %s", type(error).__name__)
        trace.degraded.append("bm25_unavailable")
        trace.channels["bm25"] = {"enabled": False, "reason": type(error).__name__}

    # --- vector channel ----------------------------------------------------
    vector_hits: list[tuple[str, float]] = []
    vector_reason = None
    store = None
    if settings.vector_configured:
        factory = vector_store_factory or (lambda s: MilvusVectorStore(s))
        embed_factory = embedding_factory or (lambda s: EmbeddingClient(s))
        try:
            store = factory(settings)
            embedder = embed_factory(settings)
            vector = embedder.embed([query])[0]
            vector_hits = store.search(vector, generation_id=str(generation.id), k=top_k)
            trace.channels["vector"] = {
                "enabled": True,
                "count": len(vector_hits),
                "top": [{"chunk_id": cid, "score": round(score, 6)} for cid, score in vector_hits[:5]],
            }
        except VectorUnavailable as error:
            vector_reason = str(error)
        except DomainError as error:
            vector_reason = error.code
        except Exception as error:  # noqa: BLE001
            vector_reason = type(error).__name__
    else:
        vector_reason = "not_configured"
    if vector_reason:
        if vector_reason != "not_configured":
            trace.degraded.append("vector_unavailable")
        trace.channels["vector"] = {"enabled": False, "reason": vector_reason}

    if "bm25_unavailable" in trace.degraded and "vector_unavailable" in trace.degraded:
        raise DomainError("RETRIEVAL_UNAVAILABLE", "检索服务当前不可用", 503, retryable=True)

    # --- RRF fusion --------------------------------------------------------
    rankings = {}
    if bm25_hits:
        rankings["bm25"] = [hit.chunk_id for hit in bm25_hits]
    if vector_hits:
        rankings["vector"] = [cid for cid, _ in vector_hits]
    fused_scores = _rrf_fuse(rankings)
    ranked_ids = sorted(fused_scores.items(), key=lambda item: (-item[1], item[0]))
    trace.fused = len(ranked_ids)
    if not ranked_ids:
        return [], trace

    # --- DB check-back: scope, state, membership, filters ------------------
    rerank_slice = [chunk_id for chunk_id, _ in ranked_ids[: settings.rag_rerank_k]]
    allowed = _db_check_back(
        db,
        repo_id=repo_id,
        generation_id=generation.id,
        candidate_ids=rerank_slice,
        source_types=source_types,
        date_range=date_range,
    )
    dropped = [cid for cid in rerank_slice if cid not in allowed]
    trace.dropped_by_filtering = dropped
    selected_ids = [cid for cid in rerank_slice if cid in allowed]
    if dropped and len(selected_ids) < (final_k or settings.rag_final_k):
        # One bounded refill from the remaining fused candidates.
        extra_candidates = [
            cid
            for cid, _ in ranked_ids[settings.rag_rerank_k : settings.rag_rerank_k * 2]
            if cid not in dropped
        ]
        extra_allowed = _db_check_back(
            db,
            repo_id=repo_id,
            generation_id=generation.id,
            candidate_ids=extra_candidates,
            source_types=source_types,
            date_range=date_range,
        )
        selected_ids.extend(cid for cid in extra_candidates if cid in extra_allowed)

    limit = final_k or settings.rag_final_k
    packed = _pack(db, selected_ids[:limit], fused_scores)
    trace.selected = [
        {"chunk_id": str(chunk.chunk_id), "rrf": round(fused_scores[str(chunk.chunk_id)], 6)}
        for chunk in packed
        if str(chunk.chunk_id) in fused_scores
    ]
    return packed, trace


def _db_check_back(
    db: DBSession,
    *,
    repo_id: UUID,
    generation_id: UUID,
    candidate_ids: list[str],
    source_types: list[str] | None,
    date_range: tuple[datetime | None, datetime | None] | None,
) -> set[str]:
    if not candidate_ids:
        return set()
    try:
        ids = [UUID(chunk_id) for chunk_id in candidate_ids]
    except ValueError:
        return set()
    members = set(
        str(row)
        for row in db.scalars(
            select(IndexMembership.chunk_id).where(
                IndexMembership.generation_id == generation_id,
                IndexMembership.chunk_id.in_(ids),
            )
        )
    )
    if not members:
        return set()
    query = (
        select(Chunk.id)
        .join(DocumentVersion, DocumentVersion.id == Chunk.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            Chunk.repo_id == repo_id,
            Chunk.id.in_([UUID(chunk_id) for chunk_id in members]),
            Document.state == "active",
        )
    )
    if source_types:
        query = query.where(Document.origin.in_(source_types))
    if date_range is not None:
        start, end = date_range
        if start is not None:
            query = query.where(Document.updated_at >= start)
        if end is not None:
            query = query.where(Document.updated_at <= end)
    return {str(chunk_id) for chunk_id in db.scalars(query)}


def _pack(db: DBSession, chunk_ids: list[str], fused_scores: dict[str, float]) -> list[RetrievedChunk]:
    if not chunk_ids:
        return []
    rows = db.execute(
        select(Chunk, DocumentVersion, Document)
        .join(DocumentVersion, DocumentVersion.id == Chunk.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(Chunk.id.in_([UUID(chunk_id) for chunk_id in chunk_ids]))
    ).all()
    by_id = {str(chunk.id): (chunk, version, document) for chunk, version, document in rows}
    packed: list[RetrievedChunk] = []
    for chunk_id in chunk_ids:
        entry = by_id.get(str(chunk_id))
        if entry is None:
            continue
        chunk, version, document = entry
        packed.append(
            RetrievedChunk(
                chunk_id=chunk.id,
                document_id=document.id,
                document_version_id=version.id,
                source_key=document.source_key,
                title=document.title,
                ordinal=chunk.ordinal,
                heading_path=chunk.heading_path or [],
                text=chunk.text,
                line_start=chunk.line_start,
                line_end=chunk.line_end,
                scores={"rrf": round(fused_scores.get(str(chunk_id), 0.0), 6)},
            )
        )
    return packed
