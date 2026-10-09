"""Immutable evidence capture (README 9.3).

Evidence stores an excerpt plus a locator; the full underlying content lives
in content-addressed object storage. Nothing is rewritten in place - a changed
source produces a new evidence row and a new object.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.core.config import Settings
from app.persistence.models import Evidence, SourceSnapshot
from app.storage.local import put_object, text_excerpt


def capture(
    db: DBSession,
    settings: Settings,
    *,
    repo_id: UUID,
    source_type: str,
    content: str | bytes,
    locator: dict | None = None,
    source_url: str | None = None,
    observed_at: datetime | None = None,
    snapshot_id: UUID | None = None,
    excerpt_limit: int = 500,
) -> Evidence:
    payload = content.encode() if isinstance(content, str) else content
    object_ref, digest = put_object(settings.storage_root, repo_id, "evidence", payload)
    text = payload.decode("utf-8", errors="replace")
    excerpt, truncated = text_excerpt(text, limit=excerpt_limit)
    row = Evidence(
        repo_id=repo_id,
        snapshot_id=snapshot_id,
        source_type=source_type,
        source_url=source_url,
        locator={**(locator or {}), "object_ref": object_ref},
        excerpt=excerpt,
        content_hash=digest,
        truncated=truncated,
        observed_at=observed_at or datetime.now(UTC),
    )
    db.add(row)
    db.flush()
    return row


def ensure_snapshot(
    db: DBSession,
    settings: Settings,
    *,
    repo_id: UUID,
    source_type: str,
    external_id: str,
    content: str | bytes,
    revision: str | None = None,
    observed_at: datetime | None = None,
) -> SourceSnapshot:
    """Store a raw source snapshot unless the identical revision already exists."""
    import hashlib

    payload = content.encode() if isinstance(content, str) else content
    digest = hashlib.sha256(payload).hexdigest()
    latest = db.scalar(
        select(SourceSnapshot)
        .where(
            SourceSnapshot.repo_id == repo_id,
            SourceSnapshot.source_type == source_type,
            SourceSnapshot.external_id == external_id,
        )
        .order_by(SourceSnapshot.created_at.desc())
        .limit(1)
    )
    if latest is not None and latest.content_hash == digest and latest.revision == revision:
        return latest
    object_ref, digest = put_object(settings.storage_root, repo_id, "snapshots", payload)
    row = SourceSnapshot(
        repo_id=repo_id,
        source_type=source_type,
        external_id=external_id,
        revision=revision,
        observed_at=observed_at or datetime.now(UTC),
        object_ref=object_ref,
        content_hash=digest,
    )
    db.add(row)
    db.flush()
    return row
