from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api.deps import current_identity, get_db, mutation_identity, repo_scope
from app.core.errors import DomainError
from app.persistence.models import Conversation, Message, Run
from app.schemas.requests import CreateConversationRequest
from app.schemas.responses import (
    conversation_view,
    decode_cursor,
    encode_cursor,
    envelope,
    message_view,
    page_limit,
    repo_view,
    run_view,
)
from app.services.repo_service import require_repo

router = APIRouter()

TERMINAL = ("completed", "failed", "cancelled", "timed_out")


def _owned_conversation(db, user, conversation_id: UUID) -> Conversation:
    conversation = db.get(Conversation, conversation_id)
    if conversation is None or conversation.owner_id != user.id:
        raise DomainError("RESOURCE_NOT_FOUND", "资源不存在", 404)
    require_repo(db, user, conversation.repo_id)
    return conversation


@router.post("/conversations", status_code=201)
def create_conversation(
    body: CreateConversationRequest,
    identity=Depends(mutation_identity),
    db=Depends(get_db),
):
    user, _ = identity
    repo = require_repo(db, user, body.repo_id)[0]
    if repo.state != "active":
        raise DomainError("REPO_DISABLED", "仓库已禁用", 409)
    conversation = Conversation(
        repo_id=repo.id,
        owner_id=user.id,
        title=(body.title or f"{repo.full_name} 会话")[:200],
    )
    db.add(conversation)
    db.commit()
    return envelope(conversation_view(conversation))


@router.get("/repos/{repo_id}/conversations")
def list_conversations(
    repo_scope_pair=Depends(repo_scope),
    identity=Depends(current_identity),
    limit: int | None = None,
    cursor: str | None = None,
    db=Depends(get_db),
):
    user, _ = identity
    repo, _ = repo_scope_pair
    query = (
        select(Conversation)
        .where(Conversation.repo_id == repo.id, Conversation.owner_id == user.id)
        .order_by(Conversation.created_at.desc(), Conversation.id.desc())
    )
    decoded = decode_cursor(cursor)
    if decoded is not None:
        created_at, row_id = decoded
        query = query.where(
            (Conversation.created_at < created_at)
            | ((Conversation.created_at == created_at) & (Conversation.id < row_id))
        )
    rows = db.scalars(query.limit(page_limit(limit) + 1)).all()
    has_more = len(rows) > page_limit(limit)
    rows = rows[: page_limit(limit)]
    next_cursor = encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None
    return envelope(
        {"items": [conversation_view(row) for row in rows], "next_cursor": next_cursor}
    )


@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: UUID,
    identity=Depends(current_identity),
    db=Depends(get_db),
):
    user, _ = identity
    conversation = _owned_conversation(db, user, conversation_id)
    repo = require_repo(db, user, conversation.repo_id)[0]
    active = db.scalar(
        select(Run)
        .where(Run.conversation_id == conversation.id, Run.status.not_in(TERMINAL))
        .order_by(Run.created_at.desc())
        .limit(1)
    )
    return envelope(
        {
            "conversation": conversation_view(conversation),
            "repo": repo_view(repo),
            "active_run": run_view(active, detail=False) if active else None,
        }
    )


@router.get("/conversations/{conversation_id}/messages")
def list_messages(
    conversation_id: UUID,
    identity=Depends(current_identity),
    after: int = 0,
    limit: int | None = None,
    db=Depends(get_db),
):
    user, _ = identity
    conversation = _owned_conversation(db, user, conversation_id)
    size = min(page_limit(limit), 200)
    rows = db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation.id, Message.seq > after)
        .order_by(Message.seq)
        .limit(size + 1)
    ).all()
    has_more = len(rows) > size
    rows = rows[:size]
    next_cursor = rows[-1].seq if has_more and rows else None
    return envelope(
        {"items": [message_view(row) for row in rows], "next_cursor": next_cursor}
    )
