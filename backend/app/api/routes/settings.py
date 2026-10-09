from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from app.agents.probe import ProbeAgent
from app.api.deps import current_identity, get_db, mutation_identity
from app.core.errors import DomainError
from app.domain.contracts import RepoScope
from app.persistence.models import Probe, WorkerHeartbeat
from app.schemas.requests import ProbeRequest
from app.schemas.responses import envelope
from app.services.auth_service import as_utc
from app.tools.registry import FIXTURE_REPO_ID, ToolRegistry

router = APIRouter()


@router.get("/settings/capabilities")
def capabilities(request: Request, identity=Depends(current_identity), db=Depends(get_db)):
    settings = request.app.state.settings
    latest = db.scalar(
        select(Probe)
        .where(Probe.user_id == identity[0].id)
        .order_by(Probe.created_at.desc())
        .limit(1)
    )
    heartbeat = db.scalar(
        select(WorkerHeartbeat).order_by(WorkerHeartbeat.last_seen_at.desc()).limit(1)
    )
    worker_online = bool(
        heartbeat and as_utc(heartbeat.last_seen_at) > datetime.now(UTC) - timedelta(seconds=30)
    )
    return envelope(
        {
            "stage": "M2",
            "configured_provider": settings.llm_provider,
            "model_configured": settings.model_configured,
            "model": settings.llm_model or None,
            "fixture_available": True,
            "worker_online": worker_online,
            "github_sync": True,
            "rag": False,
            "external_writes": False,
            "latest_probe": {
                "id": str(latest.id),
                "provider": latest.provider,
                "passed": latest.passed,
                "created_at": latest.created_at.isoformat(),
            }
            if latest
            else None,
        }
    )


@router.post("/settings/capabilities/probe")
async def probe(
    body: ProbeRequest, request: Request, identity=Depends(mutation_identity), db=Depends(get_db)
):
    settings = request.app.state.settings
    # M0 probe is a bounded protocol diagnostic, not a business/background analysis Run.
    if body.source == "configured" and not settings.model_configured:
        raise DomainError("CAPABILITY_UNAVAILABLE", "请先在 .env 配置模型服务并重启后端", 503)
    if not request.app.state.probe_lock.acquire(blocking=False):
        raise DomainError("PROBE_IN_PROGRESS", "已有模型验证正在执行", 409)
    provider = (
        "fixture"
        if body.source == "fixture" or settings.llm_provider == "fixture"
        else "openai_compatible"
    )
    scope = RepoScope(actor_id=identity[0].id, repo_id=FIXTURE_REPO_ID)
    try:
        try:
            model = request.app.state.model_factory(settings, body.source)
            result = await ProbeAgent(model, ToolRegistry(scope), settings).run(body.case_id)
            result.update(
                passed=True,
                synthetic_input=True,
                provider=provider,
                case_id=body.case_id,
                live_model_verified=provider != "fixture",
            )
        except DomainError as error:
            result = {
                "passed": False,
                "synthetic_input": True,
                "provider": provider,
                "live_model_verified": False,
                "case_id": body.case_id,
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "retryable": error.retryable,
                },
            }
        row = Probe(
            user_id=identity[0].id,
            provider=provider,
            model=settings.llm_model if provider != "fixture" else "fixture-v1",
            passed=result["passed"],
            result=result,
        )
        db.add(row)
        db.commit()
        return envelope({"id": str(row.id), **result})
    finally:
        request.app.state.probe_lock.release()


@router.get("/settings/capabilities/probes")
def probes(identity=Depends(current_identity), db=Depends(get_db)):
    rows = db.scalars(
        select(Probe)
        .where(Probe.user_id == identity[0].id)
        .order_by(Probe.created_at.desc())
        .limit(10)
    ).all()
    return envelope(
        [{"id": str(row.id), "created_at": row.created_at.isoformat(), **row.result} for row in rows]
    )
