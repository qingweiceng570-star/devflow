
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from app.api.deps import current_identity, get_db, mutation_identity, repo_scope
from app.core.errors import DomainError
from app.persistence.models import SyncRun
from app.schemas.requests import PatchRepoRequest, RegisterRepoRequest
from app.schemas.responses import (
    decode_cursor,
    encode_cursor,
    envelope,
    page_limit,
    repo_view,
    sync_run_view,
)
from app.services import repo_service

router = APIRouter()


@router.get("/repos")
def list_repos(
    limit: int | None = None,
    cursor: str | None = None,
    identity=Depends(current_identity),
    db=Depends(get_db),
):
    user, _ = identity
    rows, has_more = repo_service.visible_repos(
        db, user, cursor=decode_cursor(cursor), limit=page_limit(limit)
    )
    next_cursor = (
        encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None
    )
    return envelope({"items": [repo_view(row) for row in rows], "next_cursor": next_cursor})


@router.post("/repos", status_code=201)
def register_repo(
    body: RegisterRepoRequest,
    request: Request,
    identity=Depends(mutation_identity),
    db=Depends(get_db),
):
    user, _ = identity
    repo, created = repo_service.register_repo(
        db,
        request.app.state.settings,
        user,
        body.full_name,
        body.credential_ref,
        client_factory=request.app.state.github_client_factory,
    )
    return envelope({**repo_view(repo), "created": created})


@router.get("/repos/{repo_id}")
def get_repo(repo_scope_pair=Depends(repo_scope), db=Depends(get_db)):
    repo, _ = repo_scope_pair
    latest = db.scalar(
        select(SyncRun)
        .where(SyncRun.repo_id == repo.id)
        .order_by(SyncRun.created_at.desc())
        .limit(1)
    )
    return envelope(
        {"repo": repo_view(repo), "latest_sync": sync_run_view(latest) if latest else None}
    )


@router.patch("/repos/{repo_id}")
def patch_repo(
    body: PatchRepoRequest,
    repo_scope_pair=Depends(repo_scope),
    db=Depends(get_db),
):
    repo, _ = repo_scope_pair
    if repo.version != body.expected_version:
        raise DomainError(
            "VERSION_CONFLICT",
            "仓库配置已被修改，请刷新后重试",
            409,
            details={"current_version": repo.version},
        )
    changed = False
    if body.state is not None and body.state != repo.state:
        repo.state = body.state
        changed = True
    if body.required_checks is not None:
        config = dict(repo.config_json or {})
        config["required_checks"] = [name.strip() for name in body.required_checks if name.strip()]
        if config != (repo.config_json or {}):
            repo.config_json = config
            changed = True
    if changed:
        repo.version += 1
    db.commit()
    return envelope(repo_view(repo))
