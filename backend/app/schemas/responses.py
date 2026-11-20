import base64
import binascii
from datetime import UTC, datetime
from uuid import UUID

from app.core.errors import DomainError
from app.integrations.github import normalizers

_PAGE_DEFAULT = 20
_PAGE_MAX = 100


def iso_utc(value: datetime | None) -> str | None:
    """Serialize a datetime as an explicit UTC timestamp.

    SQLite returns naive datetimes while PostgreSQL returns aware ones; without
    this normalization the browser would interpret a naive value as local time
    and the API cursor would shift by the host timezone.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def envelope(data):
    return {"data": data, "meta": {"stage": "M7"}}


def page_limit(value: int | None) -> int:
    if value is None:
        return _PAGE_DEFAULT
    return max(1, min(value, _PAGE_MAX))


def encode_cursor(created_at: datetime, row_id: UUID) -> str:
    stamp = created_at if created_at.tzinfo else created_at.replace(tzinfo=UTC)
    raw = f"{stamp.astimezone(UTC).isoformat()}|{row_id}"
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
        "created_at": iso_utc(repo.created_at),
        "updated_at": iso_utc(repo.updated_at),
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
        "started_at": iso_utc(run.started_at),
        "finished_at": iso_utc(run.finished_at),
        "created_at": iso_utc(run.created_at),
    }


def job_view(job) -> dict:
    return {
        "id": str(job.id),
        "kind": job.kind,
        "status": job.status,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "available_at": iso_utc(job.available_at),
        "last_error": job.last_error,
        "created_at": iso_utc(job.created_at),
        "updated_at": iso_utc(job.updated_at),
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
        "observed_at": iso_utc(evidence.observed_at),
        "created_at": iso_utc(evidence.created_at),
    }


def conversation_view(conversation) -> dict:
    return {
        "id": str(conversation.id),
        "repo_id": str(conversation.repo_id),
        "title": conversation.title,
        "created_at": iso_utc(conversation.created_at),
        "updated_at": iso_utc(conversation.updated_at),
    }


def message_view(message) -> dict:
    return {
        "id": str(message.id),
        "seq": message.seq,
        "role": message.role,
        "content": message.content,
        "run_id": str(message.run_id) if message.run_id else None,
        "created_at": iso_utc(message.created_at),
    }


def run_view(run, *, detail: bool = True) -> dict:
    view = {
        "id": str(run.id),
        "repo_id": str(run.repo_id),
        "conversation_id": str(run.conversation_id) if run.conversation_id else None,
        "kind": run.kind,
        "status": run.status,
        "target": run.target or {},
        "stop_reason": run.stop_reason,
        "error": run.error,
        "cancel_requested_at": iso_utc(run.cancel_requested_at),
        "started_at": iso_utc(run.started_at),
        "finished_at": iso_utc(run.finished_at),
        "created_at": iso_utc(run.created_at),
        "updated_at": iso_utc(run.updated_at),
    }
    if detail:
        view["result"] = run.result
        view["partial_result"] = run.partial_result
        view["config_snapshot"] = run.config_snapshot or {}
    return view


def tool_call_view(row) -> dict:
    result = row.result or {}
    return {
        "id": str(row.id),
        "tool": row.tool,
        "status": row.status,
        "args": row.args or {},
        "error_code": row.error_code,
        "duration_ms": row.duration_ms,
        "evidence_ids": result.get("evidence_ids") or [],
        "created_at": iso_utc(row.created_at),
        "finished_at": iso_utc(row.finished_at),
    }


def event_view(event) -> dict:
    return {
        "schema_version": "1.0",
        "run_id": str(event.run_id),
        "seq": event.seq,
        "type": event.type,
        "payload": event.payload or {},
        "timestamp": iso_utc(event.created_at),
    }


def document_view(document) -> dict:
    return {
        "id": str(document.id),
        "repo_id": str(document.repo_id),
        "source_key": document.source_key,
        "origin": document.origin,
        "title": document.title,
        "state": document.state,
        "version": document.version,
        "byte_size": document.byte_size,
        "source_revision": document.source_revision,
        "current_version_id": str(document.current_version_id)
        if document.current_version_id
        else None,
        "created_at": iso_utc(document.created_at),
        "updated_at": iso_utc(document.updated_at),
        "deleted_at": iso_utc(document.deleted_at),
    }


def document_version_view(version) -> dict:
    return {
        "id": str(version.id),
        "content_hash": version.content_hash,
        "source_revision": version.source_revision,
        "parser_version": version.parser_version,
        "created_at": iso_utc(version.created_at),
    }
