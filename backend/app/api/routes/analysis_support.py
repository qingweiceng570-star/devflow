"""Repository-scoped selectors and explicit, read-only source diagnostics."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select

from app.api.deps import current_identity, get_db, mutation_identity, repo_scope
from app.core.errors import DomainError
from app.integrations.github.credentials import resolve_read_token
from app.integrations.github.read_adapter import GitHubRead
from app.persistence.models import CiRun, Evidence, Issue, PullRequest, Run, RunEvidence
from app.schemas.responses import envelope, evidence_view
from app.services.repo_service import require_repo

router = APIRouter()


def read_for(request, repo):
    settings = request.app.state.settings
    token = resolve_read_token(settings, repo.credential_ref)
    return GitHubRead(request.app.state.github_client_factory(settings, token), repo.full_name)


@router.get("/repos/{repo_id}/analysis-options")
def options(repo_scope_pair=Depends(repo_scope), db=Depends(get_db)):
    repo = repo_scope_pair[0]
    pulls = db.scalars(
        select(PullRequest)
        .where(PullRequest.repo_id == repo.id)
        .order_by(PullRequest.source_updated_at.desc())
        .limit(100)
    ).all()
    issues = db.scalars(
        select(Issue)
        .where(Issue.repo_id == repo.id)
        .order_by(Issue.source_updated_at.desc())
        .limit(100)
    ).all()
    ci = db.scalars(
        select(CiRun)
        .where(CiRun.repo_id == repo.id)
        .order_by(CiRun.source_created_at.desc(), CiRun.attempt.desc())
        .limit(100)
    ).all()
    return envelope(
        {
            "pulls": [
                {"number": p.number, "title": p.title, "state": p.state, "head_sha": p.head_sha}
                for p in pulls
            ],
            "issues": [{"number": i.number, "title": i.title, "state": i.state} for i in issues],
            "ci_runs": [
                {
                    "run_id": int(c.external_id),
                    "attempt": c.attempt,
                    "name": c.workflow_name,
                    "head_sha": c.head_sha,
                    "status": c.status,
                    "conclusion": c.conclusion,
                }
                for c in ci
                if c.external_id.isdigit()
            ],
            "coverage": "每类最多 100 条已同步记录；可手动填写未列出的目标编号",
        }
    )


@router.get("/repos/{repo_id}/ci-options")
def ci_options(request: Request, pr_number: int = Query(ge=1), repo_scope_pair=Depends(repo_scope)):
    read = read_for(request, repo_scope_pair[0])
    raw = read.get_pr_live(pr_number)
    head = (raw.get("head") or {}).get("sha")
    if not head:
        raise DomainError("HEAD_MISSING", "无法读取 PR 的提交", 409)
    lookup = read.ci_for_head(head, max_pages=min(3, request.app.state.settings.sync_max_pages))
    return envelope(
        {
            "head_sha": head,
            "has_more": lookup["has_more"],
            "pages": lookup["pages"],
            "items": [
                {
                    "run_id": r["id"],
                    "attempt": r.get("run_attempt", 1),
                    "name": r.get("name"),
                    "status": r.get("status"),
                    "conclusion": r.get("conclusion"),
                    "head_sha": head,
                }
                for r in lookup["items"]
            ],
        }
    )


@router.post("/repos/{repo_id}/github-diagnostics")
def diagnostics(
    request: Request,
    job_id: int | None = Query(default=None, ge=1),
    repo_scope_pair=Depends(repo_scope),
    identity=Depends(mutation_identity),
):
    read = read_for(request, repo_scope_pair[0])
    checks = []
    operations = [
        ("repository", read.get_repo),
        (
            "actions",
            lambda: read.client.get(f"/repos/{read.full_name}/actions/runs", {"per_page": 1}),
        ),
    ]
    if job_id:
        operations.append(("job_log", lambda: read.get_job_log(job_id, 8192)))
    for name, operation in operations:
        try:
            operation()
            checks.append({"capability": name, "status": "available"})
        except DomainError as error:
            checks.append(
                {
                    "capability": name,
                    "status": "unavailable",
                    "code": error.code,
                    "message": error.message,
                    "remedy": (error.details or {}).get("remedy", "额度限制时等待重置后重试"),
                }
            )
    return envelope(
        {
            "checks": checks,
            "log_access": "已检查指定 job"
            if job_id
            else "未检查日志；Actions 列表可读不代表日志可读",
            "credentials_configured": bool(read.client.token),
        }
    )


@router.get("/runs/{run_id}/evidence")
def run_evidence(run_id: UUID, identity=Depends(current_identity), db=Depends(get_db)):
    run = db.get(Run, run_id)
    if not run:
        raise DomainError("RESOURCE_NOT_FOUND", "资源不存在", 404)
    require_repo(db, identity[0], run.repo_id)
    rows = db.scalars(
        select(Evidence)
        .where(
            Evidence.id.in_(select(RunEvidence.evidence_id).where(RunEvidence.run_id == run_id)),
            Evidence.repo_id == run.repo_id,
        )
        .order_by(Evidence.created_at, Evidence.id)
        .limit(200)
    ).all()
    return envelope({"items": [evidence_view(row) for row in rows], "limit": 200})
