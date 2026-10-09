from uuid import UUID

from fastapi import APIRouter, Depends

from app.api.deps import current_identity, get_db
from app.core.errors import DomainError
from app.persistence.models import Evidence
from app.schemas.responses import envelope, evidence_view
from app.services.repo_service import require_repo

router = APIRouter()


@router.get("/evidence/{evidence_id}")
def get_evidence(
    evidence_id: UUID,
    identity=Depends(current_identity),
    db=Depends(get_db),
):
    user, _ = identity
    row = db.get(Evidence, evidence_id)
    if row is None:
        raise DomainError("RESOURCE_NOT_FOUND", "资源不存在", 404)
    require_repo(db, user, row.repo_id)
    return envelope(evidence_view(row))
