"""Evidence-backed project memory. Approval and revocation never erase history."""

import fnmatch
import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from pydantic import Field
from sqlalchemy import select

from app.core.errors import DomainError
from app.domain.contracts import Contract
from app.persistence.models import (
    AuditEvent,
    ConversationSummary,
    MemoryCandidate,
    MemoryVersion,
    Message,
    Run,
    RunEvidence,
)


class MemoryInput(Contract):
    run_id: UUID
    key: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=3000)
    evidence_ids: list[UUID] = Field(min_length=1, max_length=20)
    paths: list[str] = Field(default_factory=list, max_length=20)
    commit_sha: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    expires_at: datetime


def audit(db, repo_id, actor_id, entity_id, event, **detail):
    db.add(
        AuditEvent(
            repo_id=repo_id, actor_id=actor_id, entity_id=entity_id, event=event, detail=detail
        )
    )


def propose(db, repo_id, actor_id, body):
    run = db.get(Run, body.run_id)
    if not run or run.repo_id != repo_id or run.status != "completed":
        raise DomainError("INVALID_MEMORY_SOURCE", "记忆必须来自本仓库已完成的分析", 400)
    valid = set(db.scalars(select(RunEvidence.evidence_id).where(RunEvidence.run_id == run.id)))
    if (
        not set(body.evidence_ids) <= valid
        or not body.expires_at.tzinfo
        or body.expires_at <= datetime.now(UTC)
    ):
        raise DomainError("INVALID_MEMORY_SOURCE", "证据引用或有效期无效", 400)
    row = MemoryCandidate(
        repo_id=repo_id, actor_id=actor_id, run_id=run.id, payload=body.model_dump(mode="json")
    )
    db.add(row)
    db.flush()
    audit(db, repo_id, actor_id, row.id, "memory.proposed")
    db.commit()
    return row


def decide(db, candidate, actor_id, decision, supersedes=None):
    candidate = db.scalar(
        select(MemoryCandidate).where(MemoryCandidate.id == candidate.id).with_for_update()
    )
    if candidate.state != "pending":
        raise DomainError("STATE_CONFLICT", "候选已处理", 409)
    if decision == "approved":
        if datetime.fromisoformat(candidate.payload["expires_at"]) <= datetime.now(UTC):
            raise DomainError("MEMORY_EXPIRED", "候选已过期", 409)
        # Serialize conflicting approvals on repository row, even when no memory exists.
        from app.persistence.models import Repository

        db.scalar(select(Repository).where(Repository.id == candidate.repo_id).with_for_update())
        active = db.scalars(
            select(MemoryVersion).where(
                MemoryVersion.repo_id == candidate.repo_id, MemoryVersion.state == "active"
            )
        ).all()
        conflicts = [row for row in active if row.payload["key"] == candidate.payload["key"]]
        if {str(row.id) for row in conflicts} != set(supersedes or []):
            raise DomainError(
                "MEMORY_CONFLICT",
                "需要明确选择被替代的现有记忆",
                409,
                details={"conflicts": [str(r.id) for r in conflicts]},
            )
        for row in conflicts:
            row.state = "superseded"
            audit(db, row.repo_id, actor_id, row.id, "memory.superseded")
        version = MemoryVersion(
            candidate_id=candidate.id,
            repo_id=candidate.repo_id,
            payload=candidate.payload,
            approved_by=actor_id,
        )
        db.add(version)
    candidate.state = decision
    audit(db, candidate.repo_id, actor_id, candidate.id, "memory." + decision)
    db.commit()


def recall(db, repo_id, *, commit_sha=None, paths=()):
    rows = db.scalars(
        select(MemoryVersion)
        .where(MemoryVersion.repo_id == repo_id, MemoryVersion.state == "active")
        .order_by(MemoryVersion.created_at.desc())
        .limit(100)
    ).all()
    accepted = []
    for row in rows:
        payload = row.payload
        if datetime.fromisoformat(payload["expires_at"]) <= datetime.now(UTC):
            continue
        if payload.get("commit_sha") and payload["commit_sha"] != commit_sha:
            continue
        if payload.get("paths") and not any(
            fnmatch.fnmatch(path, pattern) for path in paths for pattern in payload["paths"]
        ):
            continue
        accepted.append({"memory_id": str(row.id), **payload})
        if len(accepted) == 5:
            break
    return accepted


def summarize(db, conversation_id, through_seq):
    rows = db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.seq <= through_seq)
        .order_by(Message.seq)
    ).all()
    fingerprint = hashlib.sha256(
        json.dumps([(r.seq, r.content) for r in rows], ensure_ascii=False).encode()
    ).hexdigest()
    existing = db.scalar(
        select(ConversationSummary).where(
            ConversationSummary.conversation_id == conversation_id,
            ConversationSummary.message_hash == fingerprint,
        )
    )
    if existing:
        return existing
    content = "对话摘录（非新的事实来源）：\n" + "\n".join(
        f"[{r.seq} {r.role}] {r.content[:200]}" for r in rows[-20:]
    )
    row = ConversationSummary(
        conversation_id=conversation_id,
        through_seq=through_seq,
        message_hash=fingerprint,
        content=content,
    )
    db.add(row)
    db.commit()
    return row
