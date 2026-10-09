"""M1 worker: heartbeat, job queue consumption and periodic lease recovery."""

import logging
import signal
import socket
import threading
import time
from datetime import UTC, datetime
from uuid import uuid4

from app.core.config import get_settings
from app.core.errors import DomainError
from app.jobs import queue
from app.jobs.dispatcher import HandlerRetry, dispatch
from app.jobs.recovery import recover_expired
from app.persistence.database import create_database
from app.persistence.models import Job, WorkerHeartbeat

log = logging.getLogger("devflow.worker")


def _heartbeat(factory, worker_id: str) -> None:
    with factory() as db:
        row = db.get(WorkerHeartbeat, worker_id)
        if row is None:
            row = WorkerHeartbeat(worker_id=worker_id, last_seen_at=datetime.now(UTC))
            db.add(row)
        row.last_seen_at = datetime.now(UTC)
        db.commit()


def _fail(factory, settings, job, epoch: int, error: dict, *, retryable: bool, retry_after=None) -> None:
    with factory() as db:
        row = db.get(Job, job.id)
        if row is None:
            return
        status = queue.fail(
            db,
            job=row,
            epoch=epoch,
            error=error,
            settings=settings,
            retryable=retryable,
            retry_after=retry_after,
        )
        db.commit()
    log.info("job_finished id=%s status=%s", job.id, status)


def run_once(factory, settings, worker_id: str) -> bool:
    """Claim and execute at most one job. Returns True when a job was processed."""
    with factory() as db:
        job = queue.claim_next(db, worker_id=worker_id, lease_seconds=settings.job_lease_seconds)
        db.commit()
    if job is None:
        return False
    epoch = job.lease_epoch
    log.info("job_claimed id=%s kind=%s attempt=%s", job.id, job.kind, job.attempt)
    try:
        result = dispatch(factory, settings, job, worker_id)
    except HandlerRetry as retry:
        _fail(
            factory,
            settings,
            job,
            epoch,
            {"code": "RETRY_SCHEDULED", "message": "任务将在稍后自动重试"},
            retryable=True,
            retry_after=retry.retry_after,
        )
        return True
    except DomainError as error:
        _fail(
            factory,
            settings,
            job,
            epoch,
            {"code": error.code, "message": error.message},
            retryable=error.retryable,
        )
        return True
    except Exception as error:  # noqa: BLE001 - the worker must survive any handler failure
        log.exception("job_crashed id=%s type=%s", job.id, type(error).__name__)
        _fail(
            factory,
            settings,
            job,
            epoch,
            {"code": "INTERNAL_ERROR", "message": "任务执行出现未预期错误"},
            retryable=False,
        )
        return True
    with factory() as db:
        row = db.get(Job, job.id)
        completed = queue.complete(db, job=row, epoch=epoch) if row is not None else False
        db.commit()
    if completed:
        summary = result.get("status") if isinstance(result, dict) else None
        log.info("job_succeeded id=%s status=%s", job.id, summary)
    else:
        log.warning("job_completion_rejected id=%s lease_lost", job.id)
    return True


def main():
    settings = get_settings()
    engine, factory = create_database(settings)
    worker_id = f"{socket.gethostname()}-{uuid4().hex[:8]}"
    stopped = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopped.set())
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log.info("M1 worker started worker_id=%s", worker_id)
    last_recovery = 0.0
    try:
        while not stopped.is_set():
            try:
                _heartbeat(factory, worker_id)
                monotonic = time.monotonic()
                if monotonic - last_recovery >= settings.worker_recovery_seconds:
                    recover_expired(factory, settings)
                    last_recovery = monotonic
                if not run_once(factory, settings, worker_id):
                    stopped.wait(settings.worker_poll_seconds)
            except Exception as error:  # noqa: BLE001 - keep the loop alive
                log.error("worker_loop_error type=%s", type(error).__name__)
                stopped.wait(settings.worker_poll_seconds)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
