import logging
import threading
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import delete, select, text

from app.agents.runtime import AgentRuntime
from app.api.deps import current_identity, get_db, mutation_identity
from app.bootstrap import create_model
from app.core.config import Settings, get_settings
from app.core.errors import DomainError
from app.domain.contracts import RepoScope
from app.persistence.database import create_database
from app.persistence.models import Probe, Session, WorkerHeartbeat
from app.schemas.requests import LoginRequest, ProbeRequest
from app.services.auth_service import as_utc, login
from app.tools.registry import FIXTURE_REPO_ID, ToolRegistry

log = logging.getLogger("devflow")


class LoginLimiter:
    """Single-API-process M0 limiter; persistent distributed limiting is a later stage."""

    def __init__(self):
        self.attempts = defaultdict(deque)
        self.lock = threading.Lock()

    def check(self, key):
        with self.lock:
            now = time.monotonic()
            for stale_key in list(self.attempts):
                queue = self.attempts[stale_key]
                while queue and queue[0] <= now - 60:
                    queue.popleft()
                if not queue:
                    del self.attempts[stale_key]
            attempts = self.attempts[key]
            if len(attempts) >= 10:
                raise DomainError(
                    "RATE_LIMITED", "登录尝试过多，请一分钟后重试", 429, retryable=True
                )
            attempts.append(now)


def envelope(data):
    return {"data": data, "meta": {"stage": "M0"}}


def user_data(user):
    return {"id": str(user.id), "username": user.username, "role": user.role}


def create_app(settings: Settings | None = None, *, session_factory=None, model_factory=None):
    settings = settings or get_settings()
    app = FastAPI(
        title="DevFlow AI", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json"
    )
    app.state.settings = settings
    if session_factory is None:
        app.state.engine, session_factory = create_database(settings)
    app.state.session_factory = session_factory
    app.state.model_factory = model_factory or create_model
    app.state.login_limiter = LoginLimiter()
    app.state.probe_lock = threading.Lock()

    @app.middleware("http")
    async def request_boundary(request: Request, call_next):
        request.state.request_id = str(uuid4())
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("origin") != settings.app_origin:
                response = JSONResponse(
                    status_code=403,
                    content={
                        "error": {
                            "code": "ORIGIN_REJECTED",
                            "message": "请求来源不被允许",
                            "retryable": False,
                        },
                        "request_id": request.state.request_id,
                    },
                )
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers.update(
            {
                "X-Request-ID": request.state.request_id,
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
                "Referrer-Policy": "same-origin",
            }
        )
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, error: DomainError):
        return JSONResponse(
            status_code=error.status,
            content={
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "retryable": error.retryable,
                },
                "request_id": request.state.request_id,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "请求字段不符合契约",
                    "retryable": False,
                    "details": [
                        {"field": ".".join(map(str, item["loc"])), "type": item["type"]}
                        for item in error.errors()
                    ],
                },
                "request_id": request.state.request_id,
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception):
        log.error(
            "unexpected_error request_id=%s type=%s", request.state.request_id, type(error).__name__
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "服务暂时无法完成请求",
                    "retryable": False,
                },
                "request_id": request.state.request_id,
            },
        )

    @app.get("/api/v1/health/live")
    def live():
        return envelope({"status": "alive", "version": "0.1.0"})

    @app.get("/api/v1/health/ready")
    def ready(db=Depends(get_db)):
        try:
            revision = db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            if revision != "0001_m0":
                raise ValueError("Schema mismatch")
        except Exception:
            raise DomainError("DATABASE_NOT_READY", "数据库不可用或尚未执行 M0 迁移", 503) from None
        return envelope({"status": "ready", "schema_revision": revision})

    @app.post("/api/v1/auth/login")
    def login_route(body: LoginRequest, request: Request, response: Response, db=Depends(get_db)):
        client = request.client.host if request.client else "unknown"
        app.state.login_limiter.check(client)
        user, token, csrf = login(db, body.username, body.password, settings.session_hours)
        cookie_options = dict(
            secure=settings.cookie_secure,
            samesite="strict",
            path="/",
            max_age=settings.session_hours * 3600,
        )
        response.set_cookie("devflow_session", token, httponly=True, **cookie_options)
        response.set_cookie("devflow_csrf", csrf, httponly=False, **cookie_options)
        return envelope(user_data(user))

    @app.get("/api/v1/auth/me")
    def me(identity=Depends(current_identity)):
        return envelope(user_data(identity[0]))

    @app.post("/api/v1/auth/logout", status_code=204)
    def logout(response: Response, identity=Depends(mutation_identity), db=Depends(get_db)):
        db.execute(delete(Session).where(Session.id == identity[1].id))
        db.commit()
        response.delete_cookie("devflow_session", path="/")
        response.delete_cookie("devflow_csrf", path="/")

    @app.get("/api/v1/settings/capabilities")
    def capabilities(identity=Depends(current_identity), db=Depends(get_db)):
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
                "stage": "M0",
                "configured_provider": settings.llm_provider,
                "model_configured": settings.model_configured,
                "model": settings.llm_model or None,
                "fixture_available": True,
                "worker_online": worker_online,
                "github_sync": False,
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

    @app.post("/api/v1/settings/capabilities/probe")
    async def probe(body: ProbeRequest, identity=Depends(mutation_identity), db=Depends(get_db)):
        # M0 probe is a bounded protocol diagnostic, not a business/background analysis Run.
        if body.source == "configured" and not settings.model_configured:
            raise DomainError("CAPABILITY_UNAVAILABLE", "请先在 .env 配置模型服务并重启后端", 503)
        if not app.state.probe_lock.acquire(blocking=False):
            raise DomainError("PROBE_IN_PROGRESS", "已有模型验证正在执行", 409)
        provider = (
            "fixture"
            if body.source == "fixture" or settings.llm_provider == "fixture"
            else "openai_compatible"
        )
        scope = RepoScope(actor_id=identity[0].id, repo_id=FIXTURE_REPO_ID)
        try:
            try:
                model = app.state.model_factory(settings, body.source)
                result = await AgentRuntime(model, ToolRegistry(scope), settings).run(body.case_id)
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
            app.state.probe_lock.release()

    @app.get("/api/v1/settings/capabilities/probes")
    def probes(identity=Depends(current_identity), db=Depends(get_db)):
        rows = db.scalars(
            select(Probe)
            .where(Probe.user_id == identity[0].id)
            .order_by(Probe.created_at.desc())
            .limit(10)
        ).all()
        return envelope(
            [
                {"id": str(row.id), "created_at": row.created_at.isoformat(), **row.result}
                for row in rows
            ]
        )

    return app


def app_factory():
    return create_app()
