from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.core.config import Settings
from app.core.errors import DomainError
from app.domain.enums import (
    ACTIVE_JOB_STATUSES,
    ACTIVE_SYNC_STATUSES,
    JobKind,
    SyncResource,
    SyncStatus,
)
from app.jobs.queue import enqueue
from app.persistence.models import Job, Repository, SyncRun


def enqueue_sync(
    db: DBSession,
    settings: Settings,
    repo: Repository,
    resources: list[str],
    mode: str = "incremental",
) -> tuple[SyncRun, bool]:
    """Create a sync run plus its job in one transaction.

    Returns an existing active run when the requested resources overlap; the
    caller gets `created=False` and the same sync id instead of a duplicate.
    """
    valid = {resource.value for resource in SyncResource}
    unknown = [r for r in resources if r not in valid]
    if unknown:
        raise DomainError("INVALID_REQUEST", f"未知同步资源: {', '.join(unknown)}", 400)
    if repo.state != "active":
        raise DomainError("REPO_DISABLED", "仓库已禁用，不能发起同步", 409)

    existing = db.scalar(
        select(SyncRun)
        .where(SyncRun.repo_id == repo.id, SyncRun.status.in_(ACTIVE_SYNC_STATUSES))
        .order_by(SyncRun.created_at.desc())
        .limit(1)
    )
    if existing is not None:
        active_job = db.scalar(
            select(Job).where(
                Job.kind == JobKind.SYNC,
                Job.entity_id == existing.id,
                Job.status.in_(ACTIVE_JOB_STATUSES),
            )
        )
        if active_job is not None:
            return existing, False
        # Stale run without a live job (worker died before recovery ran).
        now = datetime.now(UTC)
        existing.status = SyncStatus.FAILED
        existing.errors = list(existing.errors or []) + [
            {"code": "WORKER_INTERRUPTED", "message": "执行进程中断且无活动任务"}
        ]
        existing.finished_at = now
        existing.updated_at = now

    run = SyncRun(repo_id=repo.id, resources=list(resources), mode=mode, status=SyncStatus.QUEUED)
    db.add(run)
    db.flush()
    enqueue(
        db,
        kind=JobKind.SYNC,
        entity_id=run.id,
        max_attempts=settings.job_max_attempts,
    )
    db.commit()
    return run, True
