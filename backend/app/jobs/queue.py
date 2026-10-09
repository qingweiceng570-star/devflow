"""PostgreSQL table-backed job queue (README 17).

Short claim transactions with FOR UPDATE SKIP LOCKED; every completion or
failure is guarded by the lease epoch so an expired worker cannot overwrite
state written by a newer owner. SQLite tests exercise the same state machine
(locking clauses are inert there).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session as DBSession

from app.domain.enums import JobStatus
from app.persistence.models import Job


@dataclass
class RetryDecision:
    status: str
    available_at: datetime


def default_max_attempts(settings) -> int:
    return settings.job_max_attempts


def enqueue(
    db: DBSession,
    *,
    kind: str,
    entity_id: UUID,
    payload: dict | None = None,
    available_at: datetime | None = None,
    max_attempts: int | None = None,
) -> Job:
    job = Job(
        kind=kind,
        entity_id=entity_id,
        status=JobStatus.QUEUED,
        payload=payload or {},
        available_at=available_at or datetime.now(UTC),
        max_attempts=max_attempts if max_attempts is not None else 3,
    )
    db.add(job)
    db.flush()
    return job


def claim_next(db: DBSession, *, worker_id: str, lease_seconds: int) -> Job | None:
    now = datetime.now(UTC)
    stmt = (
        select(Job)
        .where(
            Job.status.in_((JobStatus.QUEUED, JobStatus.RETRY_WAIT)),
            Job.available_at <= now,
        )
        .order_by(Job.available_at, Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = db.scalar(stmt)
    if job is None:
        return None
    job.status = JobStatus.LEASED
    job.lease_owner = worker_id
    job.lease_epoch += 1
    job.lease_until = now + timedelta(seconds=lease_seconds)
    job.attempt += 1
    job.updated_at = now
    db.flush()
    return job


def renew(db_factory, *, job_id: UUID, worker_id: str, epoch: int, lease_seconds: int) -> bool:
    """Epoch-checked heartbeat on a fresh short transaction."""
    now = datetime.now(UTC)
    with db_factory() as db:
        result = db.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.lease_owner == worker_id,
                Job.lease_epoch == epoch,
                Job.status == JobStatus.LEASED,
            )
            .values(lease_until=now + timedelta(seconds=lease_seconds), updated_at=now)
        )
        db.commit()
        return result.rowcount == 1


def complete(db: DBSession, *, job: Job, epoch: int) -> bool:
    now = datetime.now(UTC)
    result = db.execute(
        update(Job)
        .where(Job.id == job.id, Job.lease_epoch == epoch, Job.status == JobStatus.LEASED)
        .values(status=JobStatus.SUCCEEDED, updated_at=now)
    )
    return result.rowcount == 1


def decide_retry(*, attempt: int, max_attempts: int, retryable: bool, retry_after: int | None, settings) -> RetryDecision:
    """Retry classification (README 17.5): only retryable errors retry, with
    bounded exponential-ish delay and an optional upstream-requested wait."""
    now = datetime.now(UTC)
    if not retryable or attempt >= max_attempts:
        return RetryDecision(status=JobStatus.FAILED, available_at=now)
    base = min(
        settings.job_retry_delay_seconds * attempt,
        settings.job_retry_max_delay_seconds,
    )
    if retry_after is not None:
        base = min(max(base, retry_after), settings.job_retry_max_delay_seconds)
    return RetryDecision(status=JobStatus.RETRY_WAIT, available_at=now + timedelta(seconds=base))


def fail(
    db: DBSession,
    *,
    job: Job,
    epoch: int,
    error: dict,
    settings,
    retryable: bool = False,
    retry_after: int | None = None,
) -> str:
    decision = decide_retry(
        attempt=job.attempt,
        max_attempts=job.max_attempts,
        retryable=retryable,
        retry_after=retry_after,
        settings=settings,
    )
    now = datetime.now(UTC)
    result = db.execute(
        update(Job)
        .where(Job.id == job.id, Job.lease_epoch == epoch, Job.status == JobStatus.LEASED)
        .values(
            status=decision.status,
            available_at=decision.available_at,
            last_error=error,
            updated_at=now,
            lease_until=None,
        )
    )
    if result.rowcount != 1:
        return "lease_lost"
    return decision.status
