"""Lease-expiry recovery (README 17.3).

Jobs whose lease expired without completion are re-queued (retryable work) or
finalized as failed; linked sync runs follow the job state so the run list
never shows a stuck "running" record. External writes would require
reconciliation instead of replay - none exist yet in M1.
"""

import logging
from datetime import UTC, datetime

from sqlalchemy import select

from app.domain.enums import JobKind, JobStatus, SyncStatus
from app.jobs.queue import decide_retry
from app.persistence.models import Job, SyncRun

log = logging.getLogger("devflow.recovery")


def _update_entity(db, job: Job, *, failed: bool, now: datetime) -> None:
    if job.kind != JobKind.SYNC:
        return
    run = db.get(SyncRun, job.entity_id)
    if run is None:
        return
    if failed:
        run.status = SyncStatus.FAILED
        run.errors = list(run.errors or []) + [
            {"code": "WORKER_INTERRUPTED", "message": "执行进程中断，重试次数已用尽"}
        ]
        run.finished_at = now
    elif run.status == SyncStatus.RUNNING:
        run.status = SyncStatus.QUEUED
    run.updated_at = now


def recover_expired(db_factory, settings) -> list[dict]:
    now = datetime.now(UTC)
    actions: list[dict] = []
    with db_factory() as db:
        jobs = db.scalars(
            select(Job)
            .where(
                Job.status == JobStatus.LEASED,
                Job.lease_until.is_not(None),
                Job.lease_until < now,
            )
            .with_for_update(skip_locked=True)
        ).all()
        for job in jobs:
            decision = decide_retry(
                attempt=job.attempt,
                max_attempts=job.max_attempts,
                retryable=True,
                retry_after=None,
                settings=settings,
            )
            job.status = decision.status
            job.available_at = decision.available_at
            job.lease_owner = None
            job.lease_until = None
            job.last_error = {"code": "WORKER_INTERRUPTED", "message": "租约过期，任务已被回收"}
            job.updated_at = now
            _update_entity(db, job, failed=decision.status == JobStatus.FAILED, now=now)
            actions.append(
                {"job_id": str(job.id), "kind": job.kind, "action": str(decision.status)}
            )
        db.commit()
    for action in actions:
        log.info("recovered job=%s kind=%s action=%s", action["job_id"], action["kind"], action["action"])
    return actions
