from app.core.config import Settings
from app.core.errors import DomainError
from app.domain.enums import JobKind
from app.ingestion.github_sync import SyncRetry, run_sync
from app.jobs.handlers.analyze import handle_analyze
from app.jobs.handlers.import_documents import handle_import_documents
from app.jobs.handlers.index import handle_index
from app.jobs.queue import HandlerRetry  # re-exported for handlers and tests
from app.services.action_service import handle_action


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
    JobKind.ANALYZE: handle_analyze,
    JobKind.INDEX: handle_index,
    JobKind.IMPORT_DOCS: handle_import_documents,
    JobKind.ACTION: handle_action,
}


def dispatch(db_factory, settings: Settings, job, worker_id: str, **kwargs) -> dict:
    handler = HANDLERS.get(job.kind)
    if handler is None:
        raise DomainError("UNKNOWN_JOB_KIND", f"未注册的任务类型: {job.kind}", 500)
    return handler(db_factory, settings, job, worker_id, **kwargs)
