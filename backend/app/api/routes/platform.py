from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from sqlalchemy import select

from app.api.deps import get_db, mutation_identity, repo_scope
from app.api.routes.analysis_support import read_for
from app.core.errors import DomainError
from app.domain.contracts import Contract
from app.persistence.models import (
    ActionDraft,
    ActionExecution,
    ActionVersion,
    AuditEvent,
    MemoryCandidate,
    MemoryVersion,
    Run,
    TaskRun,
    WeeklyReport,
)
from app.schemas.responses import envelope
from app.services import action_service as actions
from app.services import memory_service as memory

router = APIRouter()


def role(pair, allowed):
    if pair[1].role not in allowed:
        raise DomainError("FORBIDDEN", "当前仓库角色没有此权限", 403)


def scoped(db, model, row_id, repo_id):
    row = db.get(model, row_id)
    if row is None or row.repo_id != repo_id:
        raise DomainError("RESOURCE_NOT_FOUND", "资源不存在", 404)
    return row


def view(row):
    return {
        column.name: (
            str(value)
            if isinstance(value, UUID)
            else value.isoformat()
            if hasattr(value, "isoformat")
            else value
        )
        for column in row.__table__.columns
        if (value := getattr(row, column.name)) is not ...
    }


class Approval(Contract):
    version: int = Field(ge=1)
    payload_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class ActionRevision(actions.ActionInput):
    expected_version: int = Field(ge=1)


class MemoryDecision(Contract):
    decision: Literal["approved", "rejected"]
    supersedes: list[UUID] = Field(default_factory=list, max_length=100)


@router.get("/repos/{repo_id}/collaboration")
def collaboration(repo_scope_pair=Depends(repo_scope), db=Depends(get_db)):
    repo = repo_scope_pair[0]
    data = {}
    for key, model in [
        ("actions", ActionDraft),
        ("memory_candidates", MemoryCandidate),
        ("memories", MemoryVersion),
        ("reports", WeeklyReport),
        ("audit", AuditEvent),
    ]:
        data[key] = [
            view(r)
            for r in db.scalars(
                select(model)
                .where(model.repo_id == repo.id)
                .order_by(model.created_at.desc())
                .limit(100)
            )
        ]
    return envelope(data)


@router.get("/repos/{repo_id}/runs/{run_id}/tasks")
def tasks(run_id: UUID, repo_scope_pair=Depends(repo_scope), db=Depends(get_db)):
    scoped(db, Run, run_id, repo_scope_pair[0].id)
    return envelope(
        [
            view(r)
            for r in db.scalars(
                select(TaskRun).where(TaskRun.run_id == run_id).order_by(TaskRun.key)
            )
        ]
    )


@router.post("/repos/{repo_id}/memories/candidates", status_code=201)
def propose_memory(
    body: memory.MemoryInput,
    repo_scope_pair=Depends(repo_scope),
    identity=Depends(mutation_identity),
    db=Depends(get_db),
):
    role(repo_scope_pair, {"analyst", "maintainer", "admin"})
    return envelope(view(memory.propose(db, repo_scope_pair[0].id, identity[0].id, body)))


@router.post("/repos/{repo_id}/memories/candidates/{candidate_id}/decision")
def decide_memory(
    candidate_id: UUID,
    body: MemoryDecision,
    repo_scope_pair=Depends(repo_scope),
    identity=Depends(mutation_identity),
    db=Depends(get_db),
):
    role(repo_scope_pair, {"maintainer", "admin"})
    row = scoped(db, MemoryCandidate, candidate_id, repo_scope_pair[0].id)
    memory.decide(db, row, identity[0].id, body.decision, [str(v) for v in body.supersedes])
    return envelope(view(row))


@router.post("/repos/{repo_id}/memories/{memory_id}/revoke")
def revoke_memory(
    memory_id: UUID,
    repo_scope_pair=Depends(repo_scope),
    identity=Depends(mutation_identity),
    db=Depends(get_db),
):
    role(repo_scope_pair, {"maintainer", "admin"})
    row = scoped(db, MemoryVersion, memory_id, repo_scope_pair[0].id)
    row.state = "revoked"
    memory.audit(db, row.repo_id, identity[0].id, row.id, "memory.revoked")
    db.commit()
    return envelope(view(row))


@router.post("/repos/{repo_id}/actions", status_code=201)
def create_action(
    body: actions.ActionInput,
    request: Request,
    repo_scope_pair=Depends(repo_scope),
    identity=Depends(mutation_identity),
    db=Depends(get_db),
):
    role(repo_scope_pair, {"analyst", "maintainer", "admin"})
    return envelope(
        view(
            actions.create_draft(
                db,
                request.app.state.settings,
                repo_scope_pair[0],
                identity[0].id,
                body,
                client=read_for(request, repo_scope_pair[0]).client,
            )
        )
    )


@router.put("/repos/{repo_id}/actions/{draft_id}")
def revise_action(
    draft_id: UUID,
    body: ActionRevision,
    request: Request,
    repo_scope_pair=Depends(repo_scope),
    identity=Depends(mutation_identity),
    db=Depends(get_db),
):
    role(repo_scope_pair, {"analyst", "maintainer", "admin"})
    row = scoped(db, ActionDraft, draft_id, repo_scope_pair[0].id)
    row = db.scalar(
        select(ActionDraft)
        .where(ActionDraft.id == row.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row.version != body.expected_version:
        raise DomainError("VERSION_CONFLICT", "草稿已被修改，请刷新后重试", 409)
    payload = actions.ActionInput.model_validate(body.model_dump(exclude={"expected_version"}))
    return envelope(
        view(
            actions.create_draft(
                db,
                request.app.state.settings,
                repo_scope_pair[0],
                identity[0].id,
                payload,
                draft=row,
                client=read_for(request, repo_scope_pair[0]).client,
            )
        )
    )


@router.post("/repos/{repo_id}/actions/{draft_id}/approve")
def approve_action(
    draft_id: UUID,
    body: Approval,
    repo_scope_pair=Depends(repo_scope),
    identity=Depends(mutation_identity),
    db=Depends(get_db),
):
    role(repo_scope_pair, {"maintainer", "admin"})
    row = scoped(db, ActionDraft, draft_id, repo_scope_pair[0].id)
    actions.approve(db, row, identity[0].id, body.version, body.payload_hash)
    return envelope(view(row))


@router.post("/repos/{repo_id}/actions/{draft_id}/{operation}")
def operate_action(
    draft_id: UUID,
    operation: Literal["execute", "revoke", "reconcile"],
    request: Request,
    repo_scope_pair=Depends(repo_scope),
    identity=Depends(mutation_identity),
    db=Depends(get_db),
):
    role(repo_scope_pair, {"maintainer", "admin"})
    row = scoped(db, ActionDraft, draft_id, repo_scope_pair[0].id)
    if operation == "revoke":
        actions.revoke(db, row, identity[0].id)
        return envelope(view(row))
    fn = actions.execute if operation == "execute" else actions.reconcile
    return envelope(
        view(fn(db, request.app.state.settings, repo_scope_pair[0], row, identity[0].id))
    )


@router.get("/repos/{repo_id}/actions/{draft_id}")
def action_detail(draft_id: UUID, repo_scope_pair=Depends(repo_scope), db=Depends(get_db)):
    row = scoped(db, ActionDraft, draft_id, repo_scope_pair[0].id)
    execution = db.scalar(select(ActionExecution).where(ActionExecution.draft_id == row.id))
    return envelope(
        {
            "draft": view(row),
            "versions": [
                view(r)
                for r in db.scalars(
                    select(ActionVersion)
                    .where(ActionVersion.draft_id == row.id)
                    .order_by(ActionVersion.version)
                )
            ],
            "execution": view(execution) if execution else None,
        }
    )


@router.post("/repos/{repo_id}/reports/{report_id}/publish-knowledge")
def publish_report(
    report_id: UUID,
    request: Request,
    repo_scope_pair=Depends(repo_scope),
    identity=Depends(mutation_identity),
    db=Depends(get_db),
):
    import json

    from app.services.knowledge_service import upload_document

    role(repo_scope_pair, {"maintainer", "admin"})
    row = scoped(db, WeeklyReport, report_id, repo_scope_pair[0].id)
    if row.document_id is None:
        content = (
            "# 活动报告\n\n" + json.dumps(row.content, ensure_ascii=False, indent=2)
        ).encode()
        document, _ = upload_document(
            db,
            request.app.state.settings,
            repo_scope_pair[0],
            filename=f"report-{row.id}.md",
            content=content,
            origin="generated",
        )
        row.document_id = document.id
        memory.audit(
            db,
            row.repo_id,
            identity[0].id,
            row.id,
            "report.published",
            document_id=str(document.id),
        )
        db.commit()
    return envelope(view(row))
