"""Shared helpers for knowledge-base integration tests."""

from uuid import UUID

from sqlalchemy import select

from app.domain.enums import JobKind
from app.jobs.queue import claim_next
from app.persistence.models import RepoMembership, Repository, User
from app.services import knowledge_service


def make_repo(database, full_name: str = "octo/demo") -> UUID:
    with database() as db:
        user_id = db.scalar(select(User.id).where(User.username == "admin"))
        repo = Repository(
            provider="github", external_id="12345", full_name=full_name, default_branch="main"
        )
        db.add(repo)
        db.flush()
        db.add(RepoMembership(user_id=user_id, repo_id=repo.id, role="admin"))
        db.commit()
        return repo.id


def upload(database, settings, repo_id: UUID, filename: str, content: bytes, **kwargs):
    with database() as db:
        repo = db.get(Repository, repo_id)
        document, changed = knowledge_service.upload_document(
            db, settings, repo, filename=filename, content=content, **kwargs
        )
        return document.id, changed


def run_index_job(database, settings, repo_id: UUID, **handler_kwargs) -> dict:
    """Claim the pending index job, execute the real handler, complete the job
    (as the worker would)."""
    from app.jobs.handlers.index import handle_index
    from app.jobs.queue import complete
    from app.persistence.models import Job

    with database() as db:
        job = claim_next(db, worker_id="w1", lease_seconds=60)
        db.commit()
        assert job is not None and job.kind == JobKind.INDEX and job.entity_id == repo_id
        epoch = job.lease_epoch
        job_id = job.id
        result = handle_index(database, settings, job, "w1", **handler_kwargs)
        row = db.get(Job, job_id)
        complete(db, job=row, epoch=epoch)
        db.commit()
        return result


def upload_and_index(database, settings, repo_id: UUID, filename: str, text: str, **kwargs):
    document_id, _ = upload(database, settings, repo_id, filename, text.encode("utf-8"))
    result = run_index_job(database, settings, repo_id)
    return document_id, result
