"""Lease-expiry recovery (README 17.3).

Sync jobs are idempotent and are re-queued on lease expiry. Analysis runs are
not auto-retried: an interrupted run is finalized as failed with
WORKER_INTERRUPTED and the user starts a new run. External writes would
require reconciliation instead of replay - none exist yet.
"""

import logging
from datetime import UTC, datetime

from sqlalchemy import select

from app.domain.enums import JobKind, JobStatus, RunStatus, SyncStatus
from app.jobs.queue import decide_retry
from app.persistence.models import Job, Run, SyncRun

log = logging.getLogger("devflow.recovery")


def _update_entity(db, job: Job, *, failed: bool, now: datetime) -> None:
    if job.kind == JobKind.ACTION:
        from app.persistence.models import ActionDraft, ActionExecution
        from app.services.memory_service import audit
        execution = db.get(ActionExecution, job.entity_id)
        if execution and execution.state in {"queued", "executing"}:
            draft = db.get(ActionDraft, execution.draft_id)
            execution.state = draft.state = "unknown" if execution.state == "executing" else "failed"
            audit(db, draft.repo_id, None, draft.id, "action.interrupted", state=draft.state)
        return
    if job.kind == JobKind.SYNC:
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
    elif job.kind == JobKind.ANALYZE:
        run = db.get(Run, job.entity_id)
        if run is None or run.status in (RunStatus.COMPLETED, RunStatus.FAILED,
                                         RunStatus.CANCELLED, RunStatus.TIMED_OUT):
            return
        from app.agents.budget import settle_interrupted
        from app.services.run_service import finalize_run
        settle_interrupted(db, run.id)
        if run.kind == "release_check" and not failed:
            run.status = RunStatus.RECOVERING
            from app.services.run_service import emit
            emit(db, run, "run.recovering", {"reason": "lease_expired", "completed_nodes_reused": True})
            return

        marker = {"code": "WORKER_INTERRUPTED", "message": "执行进程中断"}
        finalize_run(
            db,
            run,
            status=RunStatus.FAILED,
            error=marker,
            partial_result={"note": "分析未完成；已收集的工具轨迹与证据仍可查看"},
        )


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
            # Interrupted syncs are resumed from their committed watermarks;
            # interrupted analyses are not replayed (no partial-run resume yet).
            entity = db.get(Run, job.entity_id) if job.kind == JobKind.ANALYZE else None
            retryable = job.kind in {JobKind.SYNC, JobKind.IMPORT_DOCS} or bool(entity and entity.kind == "release_check")
            decision = decide_retry(
                attempt=job.attempt,
                max_attempts=job.max_attempts,
                retryable=retryable,
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
