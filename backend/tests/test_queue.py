from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.domain.enums import JobStatus, SyncStatus
from app.jobs import queue
from app.jobs.recovery import recover_expired
from app.persistence.models import Job, Repository, SyncRun


def make_job(database, **kwargs):
    with database() as db:
        job = queue.enqueue(
            db, kind="sync", entity_id=uuid4(), available_at=kwargs.pop("available_at", None), **kwargs
        )
        db.commit()
        return job.id


def test_enqueue_claim_complete_flow(database, settings):
    job_id = make_job(database)
    with database() as db:
        job = queue.claim_next(db, worker_id="w1", lease_seconds=60)
        db.commit()
        assert job.id == job_id
        assert job.status == JobStatus.LEASED
        assert job.attempt == 1
        assert job.lease_epoch == 1
        epoch = job.lease_epoch
    with database() as db:
        assert queue.claim_next(db, worker_id="w2", lease_seconds=60) is None
    with database() as db:
        assert queue.complete(db, job=db.get(Job, job_id), epoch=epoch)
        db.commit()
    with database() as db:
        assert db.get(Job, job_id).status == JobStatus.SUCCEEDED
        # A completed job cannot be completed again.
        assert not queue.complete(db, job=db.get(Job, job_id), epoch=epoch)


def test_claim_respects_available_at(database):
    make_job(database, available_at=datetime.now(UTC) + timedelta(minutes=5))
    with database() as db:
        assert queue.claim_next(db, worker_id="w1", lease_seconds=60) is None


def test_stale_epoch_completion_is_rejected(database):
    job_id = make_job(database)
    with database() as db:
        job = queue.claim_next(db, worker_id="w1", lease_seconds=60)
        db.commit()
        stale_epoch = job.lease_epoch
    # Simulate recovery: the job is requeued and later claimed by another worker.
    with database() as db:
        row = db.get(Job, job_id)
        row.status = JobStatus.RETRY_WAIT
        row.available_at = datetime.now(UTC)
        row.lease_until = None
        db.commit()
    with database() as db:
        job = queue.claim_next(db, worker_id="w2", lease_seconds=60)
        db.commit()
        assert job.lease_epoch == stale_epoch + 1
    with database() as db:
        assert not queue.complete(db, job=db.get(Job, job_id), epoch=stale_epoch)


def test_failure_classification(database, settings):
    job_id = make_job(database)
    with database() as db:
        job = queue.claim_next(db, worker_id="w1", lease_seconds=60)
        db.commit()
        status = queue.fail(
            db, job=job, epoch=job.lease_epoch, error={"code": "X"}, settings=settings, retryable=True
        )
        db.commit()
        assert status == JobStatus.RETRY_WAIT
    with database() as db:
        row = db.get(Job, job_id)
        from app.services.auth_service import as_utc

        assert as_utc(row.available_at) > datetime.now(UTC) + timedelta(seconds=10)

    # Non-retryable errors fail immediately.
    job2 = make_job(database)
    with database() as db:
        row = queue.claim_next(db, worker_id="w1", lease_seconds=60)
        db.commit()
        assert row.id == job2
        status = queue.fail(
            db, job=row, epoch=row.lease_epoch, error={"code": "X"}, settings=settings, retryable=False
        )
        db.commit()
        assert status == JobStatus.FAILED


def test_retry_after_is_respected_and_capped(database, settings):
    settings.job_retry_max_delay_seconds = 900
    job_id = make_job(database)
    with database() as db:
        job = queue.claim_next(db, worker_id="w1", lease_seconds=60)
        db.commit()
        queue.fail(
            db,
            job=job,
            epoch=job.lease_epoch,
            error={"code": "SOURCE_RATE_LIMITED"},
            settings=settings,
            retryable=True,
            retry_after=1800,
        )
        db.commit()
    with database() as db:
        row = db.get(Job, job_id)
        delta = row.available_at.replace(tzinfo=UTC) - datetime.now(UTC)
        assert timedelta(seconds=800) < delta <= timedelta(seconds=901)


def test_attempts_exhausted_fails(database, settings):
    job_id = make_job(database, max_attempts=2)
    for _ in range(2):
        with database() as db:
            row = db.get(Job, job_id)
            row.status = JobStatus.RETRY_WAIT
            row.available_at = datetime.now(UTC)
            db.commit()
        with database() as db:
            job = queue.claim_next(db, worker_id="w1", lease_seconds=60)
            db.commit()
            status = queue.fail(
                db,
                job=job,
                epoch=job.lease_epoch,
                error={"code": "X"},
                settings=settings,
                retryable=True,
            )
            db.commit()
    assert status == JobStatus.FAILED
    with database() as db:
        assert db.get(Job, job_id).status == JobStatus.FAILED


def test_recovery_requeues_expired_lease_and_updates_run(database, settings):
    from app.jobs.queue import enqueue

    with database() as db:
        repo = Repository(provider="github", external_id="1", full_name="o/r")
        db.add(repo)
        db.flush()
        run = SyncRun(repo_id=repo.id, resources=["issues"], status=SyncStatus.RUNNING)
        db.add(run)
        db.flush()
        job = enqueue(db, kind="sync", entity_id=run.id, max_attempts=3)
        db.commit()
        run_id, job_id = run.id, job.id
    with database() as db:
        claimed = queue.claim_next(db, worker_id="w1", lease_seconds=60)
        claimed.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

    actions = recover_expired(database, settings)
    assert actions and actions[0]["action"] == "retry_wait"
    with database() as db:
        assert db.get(Job, job_id).status == JobStatus.RETRY_WAIT
        assert db.get(SyncRun, run_id).status == SyncStatus.QUEUED


def test_recovery_marks_final_failure(database, settings):
    from app.jobs.queue import enqueue

    with database() as db:
        repo = Repository(provider="github", external_id="1", full_name="o/r")
        db.add(repo)
        db.flush()
        run = SyncRun(repo_id=repo.id, resources=["issues"], status=SyncStatus.RUNNING)
        db.add(run)
        db.flush()
        job = enqueue(db, kind="sync", entity_id=run.id, max_attempts=1)
        db.commit()
        run_id, job_id = run.id, job.id
    with database() as db:
        claimed = queue.claim_next(db, worker_id="w1", lease_seconds=60)
        claimed.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

    recover_expired(database, settings)
    with database() as db:
        assert db.get(Job, job_id).status == JobStatus.FAILED
        run = db.get(SyncRun, run_id)
        assert run.status == SyncStatus.FAILED
        assert any(e["code"] == "WORKER_INTERRUPTED" for e in run.errors)


@pytest.mark.pg
def test_pg_skip_locked_allows_parallel_claims(pg_factory):
    """With PostgreSQL, a second worker skips the row the first one holds."""
    with pg_factory() as db:
        queue.enqueue(db, kind="sync", entity_id=uuid4())
        queue.enqueue(db, kind="sync", entity_id=uuid4())
        db.commit()

    session_a = pg_factory()
    session_b = pg_factory()
    try:
        job_a = queue.claim_next(session_a, worker_id="a", lease_seconds=60)
        assert job_a is not None
        # session_a's row lock is still held (no commit yet).
        job_b = queue.claim_next(session_b, worker_id="b", lease_seconds=60)
        session_b.commit()
        assert job_b is not None and job_b.id != job_a.id
        session_a.commit()
    finally:
        session_a.close()
        session_b.close()


@pytest.mark.pg
def test_pg_epoch_guard(pg_factory):
    with pg_factory() as db:
        job = queue.enqueue(db, kind="sync", entity_id=uuid4())
        db.commit()
        job_id = job.id
    with pg_factory() as db:
        claimed = queue.claim_next(db, worker_id="w1", lease_seconds=60)
        db.commit()
        epoch = claimed.lease_epoch
    with pg_factory() as db:
        row = db.get(Job, job_id)
        row.status = JobStatus.RETRY_WAIT
        row.lease_until = None
        row.available_at = datetime.now(UTC)
        db.commit()
    with pg_factory() as db:
        again = queue.claim_next(db, worker_id="w2", lease_seconds=60)
        db.commit()
        assert again.lease_epoch == epoch + 1
    with pg_factory() as db:
        row = db.get(Job, job_id)
        # Stale epoch writes must not match.
        from sqlalchemy import update

        result = db.execute(
            update(Job)
            .where(Job.id == job_id, Job.lease_epoch == epoch, Job.status == JobStatus.LEASED)
            .values(status=JobStatus.SUCCEEDED)
        )
        assert result.rowcount == 0
