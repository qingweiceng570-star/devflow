from app.core.config import Settings
from app.core.errors import DomainError
from app.domain.enums import JobKind
from app.ingestion.github_sync import SyncRetry, run_sync


class HandlerRetry(Exception):
    """Handler asks the queue to schedule a retry (retry_after in seconds)."""

    def __init__(self, retry_after: int | None = None):
        self.retry_after = retry_after


def handle_sync(db_factory, settings: Settings, job, worker_id: str, client_factory=None) -> dict:
    try:
        return run_sync(
            db_factory,
            settings,
            job.entity_id,
            worker_id=worker_id,
            job_id=job.id,
            job_epoch=job.lease_epoch,
            client_factory=client_factory,
        )
    except SyncRetry as retry:
        raise HandlerRetry(retry.retry_after) from None


HANDLERS = {
    JobKind.SYNC: handle_sync,
}


def dispatch(db_factory, settings: Settings, job, worker_id: str, **kwargs) -> dict:
    handler = HANDLERS.get(job.kind)
    if handler is None:
        raise DomainError("UNKNOWN_JOB_KIND", f"未注册的任务类型: {job.kind}", 500)
    return handler(db_factory, settings, job, worker_id, **kwargs)
