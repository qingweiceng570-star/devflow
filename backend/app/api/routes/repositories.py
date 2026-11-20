
from fastapi import APIRouter, Depends, Request
from sqlalchemy import case, func, select

from app.api.deps import admin_identity, current_identity, get_db, mutation_identity, repo_scope
from app.core.errors import DomainError
from app.persistence.models import (
    CiRun,
    ExtensionConfig,
    Issue,
    MemoryVersion,
    PullRequest,
    RepoMembership,
    SyncRun,
    User,
)
from app.schemas.requests import PatchRepoRequest, RegisterRepoRequest
from app.schemas.responses import (
    decode_cursor,
    encode_cursor,
    envelope,
    iso_utc,
    page_limit,
    repo_view,
    sync_run_view,
)
from app.services import knowledge_service, repo_service

router = APIRouter()


def _latest_syncs(db, repo_ids: list) -> dict:
    """Return the newest SyncRun per repository in two bounded queries.

    The list page used to make one detail request per repository from the
    browser; resolving the latest run here keeps it to a single call.
    """
    if not repo_ids:
        return {}
    ranked = (
        select(
            SyncRun.id.label("run_id"),
            func.row_number()
            .over(
                partition_by=SyncRun.repo_id,
                order_by=(SyncRun.created_at.desc(), SyncRun.id.desc()),
            )
            .label("position"),
        )
        .where(SyncRun.repo_id.in_(repo_ids))
        .subquery()
    )
    latest_ids = db.scalars(select(ranked.c.run_id).where(ranked.c.position == 1)).all()
    if not latest_ids:
        return {}
    rows = db.scalars(select(SyncRun).where(SyncRun.id.in_(latest_ids))).all()
    return {row.repo_id: row for row in rows}


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
    latest = _latest_syncs(db, [row.id for row in rows])
    items = [
        {
            **repo_view(row),
            "latest_sync": sync_run_view(latest[row.id]) if row.id in latest else None,
        }
        for row in rows
    ]
    return envelope({"items": items, "next_cursor": next_cursor})


@router.post("/repos", status_code=201)
def register_repo(
    body: RegisterRepoRequest,
    request: Request,
    identity=Depends(admin_identity),
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
    if created:
        knowledge_service.request_repository_import(db, repo)
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


@router.get("/repos/{repo_id}/workspace")
def repo_workspace(repo_scope_pair=Depends(repo_scope), db=Depends(get_db)):
    """One read for the project workspace: KPI counts, issue/PR/CI lists,
    members, memory, knowledge and extension state."""
    repo = repo_scope_pair[0]
    open_issue = Issue.state == "open"
    open_pull = PullRequest.state == "open"

    def count(model, *conditions):
        return (
            db.scalar(
                select(func.count()).select_from(model).where(model.repo_id == repo.id, *conditions)
            )
            or 0
        )

    closed_labels = db.scalars(
        select(Issue.labels).where(Issue.repo_id == repo.id, Issue.state == "closed")
    ).all()
    rejected_labels = {"wontfix", "invalid", "duplicate", "not planned"}
    rejected = sum(
        1
        for labels in closed_labels
        if labels and {str(value).lower() for value in labels} & rejected_labels
    )
    counts = {
        "open_issues": count(Issue, open_issue),
        "open_pulls": count(PullRequest, open_pull),
        "closed_issues": count(Issue, Issue.state == "closed"),
        "rejected_issues": rejected,
        "failed_ci": count(CiRun, CiRun.conclusion == "failure"),
        "merged_pulls": count(PullRequest, PullRequest.merged.is_(True)),
    }

    issues = db.scalars(
        select(Issue)
        .where(Issue.repo_id == repo.id)
        .order_by(case((open_issue, 0), else_=1), Issue.source_updated_at.desc())
        .limit(50)
    ).all()
    pulls = db.scalars(
        select(PullRequest)
        .where(PullRequest.repo_id == repo.id)
        .order_by(case((open_pull, 0), else_=1), PullRequest.source_updated_at.desc())
        .limit(30)
    ).all()
    ci_rows = db.scalars(
        select(CiRun)
        .where(CiRun.repo_id == repo.id)
        .order_by(CiRun.source_created_at.desc(), CiRun.attempt.desc())
        .limit(20)
    ).all()
    team = db.execute(
        select(User.username, RepoMembership.role)
        .join(RepoMembership, RepoMembership.user_id == User.id)
        .where(RepoMembership.repo_id == repo.id)
        .order_by(RepoMembership.created_at)
    ).all()
    memories = db.scalars(
        select(MemoryVersion)
        .where(MemoryVersion.repo_id == repo.id, MemoryVersion.state == "active")
        .order_by(MemoryVersion.created_at.desc())
        .limit(20)
    ).all()
    extensions = db.scalars(
        select(ExtensionConfig)
        .where(ExtensionConfig.repo_id == repo.id)
        .order_by(ExtensionConfig.created_at.desc())
        .limit(20)
    ).all()
    latest = _latest_syncs(db, [repo.id]).get(repo.id)

    return envelope(
        {
            "repo": repo_view(repo),
            "counts": counts,
            "issues": [
                {
                    "number": issue.number,
                    "title": issue.title,
                    "state": issue.state,
                    "labels": list(issue.labels or []),
                    "author": issue.author_login,
                    "updated_at": iso_utc(issue.source_updated_at),
                }
                for issue in issues
            ],
            "pulls": [
                {
                    "number": pull.number,
                    "title": pull.title,
                    "state": pull.state,
                    "merged": pull.merged,
                    "draft": pull.draft,
                    "author": pull.author_login,
                    "updated_at": iso_utc(pull.source_updated_at),
                }
                for pull in pulls
            ],
            "ci": [
                {
                    "run_id": int(run.external_id) if run.external_id.isdigit() else None,
                    "attempt": run.attempt,
                    "name": run.workflow_name,
                    "status": run.status,
                    "conclusion": run.conclusion,
                    "head_sha": run.head_sha,
                    "created_at": iso_utc(run.source_created_at),
                }
                for run in ci_rows
            ],
            "team": [{"username": username, "role": role} for username, role in team],
            "memories": [
                {
                    "id": str(row.id),
                    "key": (row.payload or {}).get("key"),
                    "content": (row.payload or {}).get("content"),
                    "paths": (row.payload or {}).get("paths") or [],
                }
                for row in memories
            ],
            "knowledge": knowledge_service.index_status(db, repo.id),
            "extensions": [
                {
                    "id": str(row.id),
                    "name": row.name,
                    "kind": row.kind,
                    "version": row.version,
                    "state": row.state,
                }
                for row in extensions
            ],
            "latest_sync": sync_run_view(latest) if latest else None,
        }
    )


@router.patch("/repos/{repo_id}")
def patch_repo(
    body: PatchRepoRequest,
    repo_scope_pair=Depends(repo_scope),
    db=Depends(get_db),
    identity=Depends(mutation_identity),
):
    repo, membership = repo_scope_pair
    if membership.role != "admin":
        raise DomainError("FORBIDDEN", "修改仓库策略需要仓库管理员权限", 403)
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
