"""Import a pinned repository document snapshot, then enqueue its index build."""

from datetime import UTC, datetime

from sqlalchemy import select

from app.core.errors import DomainError
from app.ingestion.repository_documents import read_document_snapshot
from app.integrations.github.credentials import resolve_read_token
from app.jobs.queue import LeaseLost, renew
from app.persistence.models import Document, Job, Repository
from app.services import knowledge_service
from app.services.auth_service import as_utc
from app.workspace.manager import CLONE_TIMEOUT_SECONDS, LOCAL_TIMEOUT_SECONDS, WorkspaceManager


def handle_import_documents(db_factory, settings, job, worker_id, *, workspace_factory=None):
    def check_stop():
        # A clone can take longer than the normal worker lease. Renew before
        # each bounded Git operation, including the initial clone.
        if not renew(
            db_factory, job_id=job.id, worker_id=worker_id, epoch=job.lease_epoch,
            lease_seconds=max(settings.job_lease_seconds, CLONE_TIMEOUT_SECONDS + LOCAL_TIMEOUT_SECONDS + 30),
        ):
            raise LeaseLost()

    with db_factory() as db:
        repo = db.get(Repository, job.entity_id)
        if repo is None or repo.state != "active":
            raise DomainError("REPO_DISABLED", "仓库不可用，不能导入文档", 409)
        token = resolve_read_token(settings, repo.credential_ref)
    manager = (workspace_factory or (lambda s, t: WorkspaceManager(s, token=t)))(settings, token)
    try:
        snapshot = read_document_snapshot(settings, manager, repo, check_stop=check_stop)
    except DomainError as error:
        if error.code != "CODE_UNAVAILABLE":
            raise
        raise DomainError(
            "DOCUMENT_SOURCE_UNAVAILABLE",
            "无法读取仓库文档，请确认 GitHub 链接和读取权限，并检查网络及 GITHUB_GIT_PROXY 配置",
            502, retryable=error.retryable,
        ) from None
    check_stop()
    with db_factory() as db:
        # Fence the whole publication transaction. An interrupted worker
        # cannot publish a partial snapshot or overwrite a newer import.
        row = db.scalar(select(Job).where(Job.id == job.id).with_for_update())
        if (
            row is None or row.status != "leased" or row.lease_owner != worker_id
            or row.lease_epoch != job.lease_epoch or not row.lease_until
            or as_utc(row.lease_until) <= datetime.now(UTC)
        ):
            raise LeaseLost()
        current_repo = db.scalar(select(Repository).where(Repository.id == repo.id).with_for_update())
        if current_repo is None or current_repo.state != "active":
            raise DomainError("REPO_DISABLED", "仓库已禁用，不能导入文档", 409)
        changed = unchanged = removed = 0
        for document in snapshot.documents:
            _, updated = knowledge_service.upload_document(
                db, settings, current_repo, filename=document.parser_filename,
                content=document.content, source_key="git:" + document.path,
                source_revision=snapshot.commit_sha, commit=False,
            )
            changed += int(updated)
            unchanged += int(not updated)
        imported = db.scalars(select(Document).where(
            Document.repo_id == repo.id, Document.source_key.startswith("git:"),
            Document.state == "active",
        )).all()
        for document in imported:
            if document.source_key[4:] in snapshot.present_paths:
                continue
            document.state = "deleted"
            document.version += 1
            document.deleted_at = document.updated_at = datetime.now(UTC)
            knowledge_service._outbox(db, repo.id, "document_deleted", document.id)
            removed += 1
        if removed:
            knowledge_service._enqueue_index_job(db, repo.id, settings)
        result = {
            "commit_sha": snapshot.commit_sha, "discovered": snapshot.discovered,
            "imported": changed, "unchanged": unchanged, "removed": removed,
            "skipped_count": len(snapshot.skipped), "skipped": snapshot.skipped[:20],
        }
        row.payload = {**(row.payload or {}), "result": result}
        db.commit()
    return {"status": "imported", **result}
