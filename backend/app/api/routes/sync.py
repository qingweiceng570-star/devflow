from uuid import UUID

from fastapi import APIRouter, Depends, Request

from app.api.deps import current_identity, get_db, mutation_identity, repo_scope
from app.api.idempotency import request_hash, run_idempotent
from app.core.errors import DomainError
from app.persistence.models import SyncRun
from app.schemas.requests import SyncRequest
from app.schemas.responses import envelope, sync_run_view
from app.services.repo_service import require_repo
from app.services.sync_service import enqueue_sync

router = APIRouter()

SYNC_ROUTE = "POST /repos/{repo_id}/sync"


@router.post("/repos/{repo_id}/sync", status_code=202)
def trigger_sync(
    body: SyncRequest,
    request: Request,
    repo_scope_pair=Depends(repo_scope),
    identity=Depends(mutation_identity),
    db=Depends(get_db),
):
    repo, _ = repo_scope_pair
    user, _ = identity
    settings = request.app.state.settings

    def create() -> dict:
        run, created = enqueue_sync(
            db, settings, repo, [r.value for r in body.resources], body.mode
        )
        return {**sync_run_view(run), "created": created}

    key = request.headers.get("Idempotency-Key")
    if key:
        data = run_idempotent(
            db,
            actor_id=user.id,
            route=SYNC_ROUTE,
            key=key,
            body_hash=request_hash(body),
            create=create,
        )
    else:
        data = create()
    return envelope(data)


@router.get("/sync-runs/{sync_id}")
def get_sync_run(
    sync_id: UUID,
    identity=Depends(current_identity),
    db=Depends(get_db),
):
    user, _ = identity
    run = db.get(SyncRun, sync_id)
    if run is None:
        raise DomainError("RESOURCE_NOT_FOUND", "资源不存在", 404)
    require_repo(db, user, run.repo_id)
    return envelope(sync_run_view(run))
