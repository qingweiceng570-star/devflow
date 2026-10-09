"""Knowledge-base tool: hybrid retrieval with citations (README 10.3, 13).

Results always carry chunk identity, scores, the fixed generation and any
degraded channels; every returned chunk becomes evidence so the model can
cite exact passages.
"""

from datetime import datetime
from typing import Literal

from pydantic import Field

from app.core.errors import DomainError
from app.domain.contracts import Contract, ToolResult
from app.rag.retriever import retrieve
from app.tools.executor import ToolDeps, ok_result

CHUNK_TEXT_CHARS = 1200


class SearchKnowledgeInput(Contract):
    query: str = Field(min_length=1, max_length=512)
    source_types: list[Literal["source", "generated"]] = Field(default_factory=list, max_length=4)
    date_from: str | None = Field(default=None, max_length=30)
    date_to: str | None = Field(default=None, max_length=30)


def _parse_bound(value: str | None, label: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise DomainError("INVALID_TOOL_ARGUMENTS", f"{label} 不是有效的 ISO 日期", 400) from None
    return parsed


async def search_knowledge(deps: ToolDeps, args: SearchKnowledgeInput) -> ToolResult:
    start = _parse_bound(args.date_from, "date_from")
    end = _parse_bound(args.date_to, "date_to")
    with deps.db_factory() as db:
        results, trace = retrieve(
            db,
            deps.settings,
            repo_id=deps.repo.id,
            query=args.query,
            source_types=list(args.source_types) or None,
            date_range=(start, end) if (start or end) else None,
            vector_store_factory=deps.scratch.get("vector_store_factory"),
            embedding_factory=deps.scratch.get("embedding_factory"),
        )
        chunks = []
        evidence_ids = []
        for item in results:
            evidence_id = deps.evidence(
                source_type="document_chunk",
                content=item.text,
                locator={
                    "document_id": str(item.document_id),
                    "document_version_id": str(item.document_version_id),
                    "chunk_id": str(item.chunk_id),
                    "ordinal": item.ordinal,
                    "heading_path": item.heading_path,
                    "line_start": item.line_start,
                    "line_end": item.line_end,
                    "generation_id": trace.generation_id,
                },
            )
            evidence_ids.append(evidence_id)
            chunks.append(
                {
                    "chunk_id": str(item.chunk_id),
                    "document": item.source_key,
                    "title": item.title,
                    "heading_path": item.heading_path,
                    "lines": [item.line_start, item.line_end],
                    "score": item.scores.get("rrf"),
                    "evidence_id": str(evidence_id),
                    "text": item.text[:CHUNK_TEXT_CHARS],
                }
            )
    data = {
        "chunks": chunks,
        "generation_id": trace.generation_id,
        "degraded": trace.degraded,
        "channels": {
            name: {"enabled": channel.get("enabled"), "count": channel.get("count")}
            for name, channel in trace.channels.items()
        },
        "fused_candidates": trace.fused,
    }
    if not chunks:
        return ok_result(
            data,
            source="knowledge_base",
            status="partial",
            warnings=["EMPTY_RECALL"] + trace.degraded,
        )
    return ok_result(
        data,
        source="knowledge_base",
        evidence_ids=evidence_ids,
        status="partial" if trace.degraded else "ok",
        warnings=trace.degraded,
    )
