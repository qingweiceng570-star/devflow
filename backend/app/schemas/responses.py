import base64
import binascii
from datetime import datetime
from uuid import UUID

from app.core.errors import DomainError
from app.integrations.github import normalizers

_PAGE_DEFAULT = 20
_PAGE_MAX = 100


def envelope(data):
    return {"data": data, "meta": {"stage": "M3"}}


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


def conversation_view(conversation) -> dict:
    return {
        "id": str(conversation.id),
        "repo_id": str(conversation.repo_id),
        "title": conversation.title,
        "created_at": conversation.created_at.isoformat(),
        "updated_at": conversation.updated_at.isoformat(),
    }


def message_view(message) -> dict:
    return {
        "id": str(message.id),
        "seq": message.seq,
        "role": message.role,
        "content": message.content,
        "run_id": str(message.run_id) if message.run_id else None,
        "created_at": message.created_at.isoformat(),
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
        "cancel_requested_at": run.cancel_requested_at.isoformat()
        if run.cancel_requested_at
        else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
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
        "created_at": row.created_at.isoformat(),
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


def event_view(event) -> dict:
    return {
        "schema_version": "1.0",
        "run_id": str(event.run_id),
        "seq": event.seq,
        "type": event.type,
        "payload": event.payload or {},
        "timestamp": event.created_at.isoformat(),
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
        "created_at": document.created_at.isoformat(),
        "updated_at": document.updated_at.isoformat(),
        "deleted_at": document.deleted_at.isoformat() if document.deleted_at else None,
    }


def document_version_view(version) -> dict:
    return {
        "id": str(version.id),
        "content_hash": version.content_hash,
        "source_revision": version.source_revision,
        "parser_version": version.parser_version,
        "created_at": version.created_at.isoformat(),
    }
