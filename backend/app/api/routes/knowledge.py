from datetime import datetime

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_db, mutation_identity, repo_scope
from app.core.errors import DomainError
from app.rag.retriever import retrieve
from app.schemas.requests import KnowledgeSearchRequest, ReindexRequest
from app.schemas.responses import envelope
from app.services import knowledge_service

router = APIRouter()


def _parse_bound(value: str | None, label: str) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise DomainError("INVALID_REQUEST", f"{label} 不是有效的 ISO 日期", 400) from None


@router.post("/repos/{repo_id}/knowledge/search")
def knowledge_search(
    body: KnowledgeSearchRequest,
    request: Request,
    repo_scope_pair=Depends(repo_scope),
    db=Depends(get_db),
):
    """Retrieval only (README 13.5): no run, no evidence rows; the response
    carries the full trace for the recall test page - channels, fusion,
    filtering and degradation are all visible."""
    repo, _ = repo_scope_pair
    start = _parse_bound(body.date_from, "date_from")
    end = _parse_bound(body.date_to, "date_to")
    results, trace = retrieve(
        db,
        request.app.state.settings,
        repo_id=repo.id,
        query=body.query,
        source_types=list(body.source_types) or None,
        date_range=(start, end) if (start or end) else None,
    )
    return envelope(
        {
            "results": [
                {
                    "chunk_id": str(item.chunk_id),
                    "document": item.source_key,
                    "title": item.title,
                    "heading_path": item.heading_path,
                    "lines": [item.line_start, item.line_end],
                    "score": item.scores.get("rrf"),
                    "text": item.text[:1200],
                }
                for item in results
            ],
            "trace": trace.as_dict(),
        }
    )


@router.get("/repos/{repo_id}/knowledge/status")
def knowledge_status(
    repo_scope_pair=Depends(repo_scope),
    db=Depends(get_db),
):
    repo, _ = repo_scope_pair
    return envelope(knowledge_service.index_status(db, repo.id))


@router.post("/repos/{repo_id}/knowledge/reindex", status_code=202)
def knowledge_reindex(
    body: ReindexRequest,
    request: Request,
    repo_scope_pair=Depends(repo_scope),
    identity=Depends(mutation_identity),
    db=Depends(get_db),
):
    repo, _ = repo_scope_pair
    knowledge_service.request_reindex(db, request.app.state.settings, repo)
    return envelope(
        {
            "accepted": True,
            "reason": body.reason,
            **knowledge_service.index_status(db, repo.id),
        }
    )
