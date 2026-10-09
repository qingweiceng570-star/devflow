from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.api import sse
from app.api.deps import current_identity, get_db, mutation_identity, repo_scope
from app.api.idempotency import request_hash, run_idempotent
from app.core.errors import DomainError
from app.persistence.models import Conversation, Run, ToolCall
from app.schemas.requests import SubmitRunRequest
from app.schemas.responses import (
    decode_cursor,
    encode_cursor,
    envelope,
    page_limit,
    run_view,
    tool_call_view,
)
from app.services import run_service
from app.services.auth_service import authenticate
from app.services.repo_service import require_repo

router = APIRouter()

SUBMIT_ROUTE = "POST /conversations/{conversation_id}/runs"


@router.post("/conversations/{conversation_id}/runs", status_code=202)
def submit_run(
    conversation_id: UUID,
    body: SubmitRunRequest,
    request: Request,
    identity=Depends(mutation_identity),
    db=Depends(get_db),
):
    user, _ = identity
    conversation = db.get(Conversation, conversation_id)
    if conversation is None or conversation.owner_id != user.id:
        raise DomainError("RESOURCE_NOT_FOUND", "资源不存在", 404)
    repo = require_repo(db, user, conversation.repo_id)[0]
    settings = request.app.state.settings

    def create() -> dict:
        run = run_service.submit_run(
            db,
            settings,
            conversation=conversation,
            repo=repo,
            message_text=body.message,
            kind=body.kind,
            target=(body.target.model_dump() if body.target else {}),
        )
        view = run_view(run, detail=False)
        return {
            **view,
            "events_url": f"/api/v1/runs/{run.id}/events",
            "result_url": f"/api/v1/runs/{run.id}",
        }

    key = request.headers.get("Idempotency-Key")
    if key:
        data = run_idempotent(
            db,
            actor_id=user.id,
            route=SUBMIT_ROUTE,
            key=key,
            body_hash=request_hash(body),
            create=create,
        )
    else:
        data = create()
    return envelope(data)


@router.get("/repos/{repo_id}/runs")
def list_runs(
    repo_scope_pair=Depends(repo_scope),
    limit: int | None = None,
    cursor: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    db=Depends(get_db),
):
    repo, _ = repo_scope_pair
    query = (
        select(Run)
        .where(Run.repo_id == repo.id)
        .order_by(Run.created_at.desc(), Run.id.desc())
    )
    if kind:
        query = query.where(Run.kind == kind)
    if status:
        query = query.where(Run.status == status)
    decoded = decode_cursor(cursor)
    if decoded is not None:
        created_at, row_id = decoded
        query = query.where(
            (Run.created_at < created_at)
            | ((Run.created_at == created_at) & (Run.id < row_id))
        )
    rows = db.scalars(query.limit(page_limit(limit) + 1)).all()
    has_more = len(rows) > page_limit(limit)
    rows = rows[: page_limit(limit)]
    next_cursor = encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None
    return envelope(
        {
            "items": [run_view(row, detail=False) for row in rows],
            "next_cursor": next_cursor,
        }
    )


def _scoped_run(db, user, run_id: UUID) -> Run:
    run = db.get(Run, run_id)
    if run is None:
        raise DomainError("RESOURCE_NOT_FOUND", "资源不存在", 404)
    require_repo(db, user, run.repo_id)
    return run


@router.get("/runs/{run_id}")
def get_run(
    run_id: UUID,
    identity=Depends(current_identity),
    db=Depends(get_db),
):
    user, _ = identity
    run = _scoped_run(db, user, run_id)
    return envelope(run_view(run))


@router.get("/runs/{run_id}/tools")
def list_tool_calls(
    run_id: UUID,
    identity=Depends(current_identity),
    db=Depends(get_db),
):
    user, _ = identity
    run = _scoped_run(db, user, run_id)
    rows = db.scalars(
        select(ToolCall)
        .where(ToolCall.run_id == run.id)
        .order_by(ToolCall.created_at, ToolCall.id)
        .limit(200)
    ).all()
    return envelope([tool_call_view(row) for row in rows])


@router.post("/runs/{run_id}/cancel", status_code=202)
def cancel_run(
    run_id: UUID,
    identity=Depends(mutation_identity),
    db=Depends(get_db),
):
    user, _ = identity
    run = _scoped_run(db, user, run_id)
    run = run_service.request_cancel(db, run)
    return envelope(run_view(run, detail=False))


@router.get("/runs/{run_id}/events")
def stream_run_events(
    run_id: UUID,
    request: Request,
    after: int | None = None,
):
    """SSE（README 19）：先补发 after 之后的事件，再跟踪到终态。

    The session is opened only for authorization and closed before streaming;
    a yielded FastAPI dependency would keep a database connection for the
    whole stream lifetime.
    """
    after_seq = sse.resolve_after(request, after)
    factory = request.app.state.session_factory
    with factory() as db:
        user, _ = authenticate(db, request.cookies.get("devflow_session"))
        run = db.get(Run, run_id)
        if run is None:
            raise DomainError("RESOURCE_NOT_FOUND", "资源不存在", 404)
        require_repo(db, user, run.repo_id)
    return StreamingResponse(
        sse.stream(factory, run_id, after_seq),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
