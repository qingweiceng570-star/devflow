import base64
import binascii
from datetime import datetime
from uuid import UUID

from app.core.errors import DomainError
from app.integrations.github import normalizers

_PAGE_DEFAULT = 20
_PAGE_MAX = 100


def envelope(data):
    return {"data": data, "meta": {"stage": "M1"}}


def page_limit(value: int | None) -> int:
    if value is None:
        return _PAGE_DEFAULT
    return max(1, min(value, _PAGE_MAX))


def encode_cursor(created_at: datetime, row_id: UUID) -> str:
    raw = f"{created_at.isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        stamp, _, row_id = raw.rpartition("|")
        return normalizers.parse_ts(stamp), UUID(row_id)
    except (ValueError, binascii.Error, AttributeError):
        raise DomainError("INVALID_REQUEST", "分页游标不合法", 400) from None


def repo_view(repo) -> dict:
    return {
        "id": str(repo.id),
        "provider": repo.provider,
        "full_name": repo.full_name,
        "default_branch": repo.default_branch,
        "state": repo.state,
        "version": repo.version,
        "created_at": repo.created_at.isoformat(),
        "updated_at": repo.updated_at.isoformat(),
    }


def sync_run_view(run) -> dict:
    return {
        "id": str(run.id),
        "repo_id": str(run.repo_id),
        "resources": list(run.resources or []),
        "mode": run.mode,
        "status": run.status,
        "counts": run.counts or {},
        "errors": run.errors or [],
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "created_at": run.created_at.isoformat(),
    }


def job_view(job) -> dict:
    return {
        "id": str(job.id),
        "kind": job.kind,
        "status": job.status,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "available_at": job.available_at.isoformat(),
        "last_error": job.last_error,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
    }


def evidence_view(evidence) -> dict:
    return {
        "id": str(evidence.id),
        "repo_id": str(evidence.repo_id),
        "snapshot_id": str(evidence.snapshot_id) if evidence.snapshot_id else None,
        "source_type": evidence.source_type,
        "source_url": evidence.source_url,
        "locator": evidence.locator or {},
        "excerpt": evidence.excerpt,
        "content_hash": evidence.content_hash,
        "truncated": evidence.truncated,
        "observed_at": evidence.observed_at.isoformat(),
        "created_at": evidence.created_at.isoformat(),
    }
