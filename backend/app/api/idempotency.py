"""Idempotency-Key handling for job-creating POSTs (README 18.1).

Same key + same body replays the stored response; same key with a different
body is rejected. Records expire after 24 hours while execution uniqueness
constraints keep applying independently.
"""

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.core.errors import DomainError
from app.persistence.models import IdempotencyRecord

TTL = timedelta(hours=24)


def request_hash(body) -> str:
    return hashlib.sha256(body.model_dump_json().encode()).hexdigest()


def run_idempotent(
    db: DBSession,
    *,
    actor_id: UUID,
    route: str,
    key: str,
    body_hash: str,
    create,
) -> dict:
    """Execute `create()` unless this (actor, route, key) already completed.

    `create` must commit its own work before returning the response payload.
    """
    now = datetime.now(UTC)
    record = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.actor_id == actor_id,
            IdempotencyRecord.route == route,
            IdempotencyRecord.key == key,
        )
    )
    if record is not None and record.expires_at.replace(tzinfo=UTC) > now:
        if record.request_hash != body_hash:
            raise DomainError("IDEMPOTENCY_MISMATCH", "相同幂等键的请求内容不一致", 409)
        if record.response is not None:
            return record.response
    response = create()
    if record is None:
        record = IdempotencyRecord(
            actor_id=actor_id, route=route, key=key, request_hash=body_hash
        )
        db.add(record)
    record.request_hash = body_hash
    record.response = response
    record.expires_at = now + TTL
    db.commit()
    return response
