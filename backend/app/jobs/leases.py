"""Resource-scoped leases: one active sync (or workspace use) per
(repo, kind, key); expired leases can be taken over with a new epoch."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from app.persistence.models import ResourceLease


def acquire(
    db: DBSession,
    *,
    repo_id: UUID,
    resource_kind: str,
    resource_key: str,
    owner: str,
    ttl_seconds: int,
) -> bool:
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=ttl_seconds)
    row = db.get(ResourceLease, (repo_id, resource_kind, resource_key), with_for_update=True)
    if row is None:
        row = ResourceLease(
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_key=resource_key,
            owner=owner,
            epoch=1,
            expires_at=expires,
        )
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            return False
        return True
    current_owner = row.owner
    current_expires = row.expires_at
    if current_expires.tzinfo is None:
        current_expires = current_expires.replace(tzinfo=UTC)
    if current_owner == owner:
        row.expires_at = expires
        row.updated_at = now
        db.flush()
        return True
    if current_expires <= now:
        row.owner = owner
        row.epoch += 1
        row.expires_at = expires
        row.updated_at = now
        db.flush()
        return True
    return False


def release(db: DBSession, *, repo_id: UUID, resource_kind: str, resource_key: str, owner: str) -> None:
    db.execute(
        delete(ResourceLease).where(
            ResourceLease.repo_id == repo_id,
            ResourceLease.resource_kind == resource_kind,
            ResourceLease.resource_key == resource_key,
            ResourceLease.owner == owner,
        )
    )


def find_owner(db: DBSession, *, repo_id: UUID, resource_kind: str, resource_key: str) -> str | None:
    row = db.scalar(
        select(ResourceLease).where(
            ResourceLease.repo_id == repo_id,
            ResourceLease.resource_kind == resource_kind,
            ResourceLease.resource_key == resource_key,
        )
    )
    return row.owner if row else None
