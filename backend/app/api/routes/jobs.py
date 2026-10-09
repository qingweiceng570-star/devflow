from uuid import UUID

from fastapi import APIRouter, Depends

from app.api.deps import current_identity, get_db
from app.core.errors import DomainError
from app.domain.enums import JobKind
from app.persistence.models import Job, SyncRun
from app.schemas.responses import envelope, job_view
from app.services.repo_service import require_repo

router = APIRouter()


@router.get("/jobs/{job_id}")
def get_job(
    job_id: UUID,
    identity=Depends(current_identity),
    db=Depends(get_db),
):
    user, _ = identity
    job = db.get(Job, job_id)
    if job is None:
        raise DomainError("RESOURCE_NOT_FOUND", "资源不存在", 404)
    if job.kind == JobKind.SYNC:
        run = db.get(SyncRun, job.entity_id)
        if run is None:
            raise DomainError("RESOURCE_NOT_FOUND", "资源不存在", 404)
        require_repo(db, user, run.repo_id)
    else:
        raise DomainError("RESOURCE_NOT_FOUND", "资源不存在", 404)
    return envelope(job_view(job))
