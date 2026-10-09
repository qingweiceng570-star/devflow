import logging
import threading
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.routes import (
    auth,
    conversations,
    documents,
    evidence,
    health,
    jobs,
    knowledge,
    repositories,
    runs,
    sync,
)
from app.api.routes import settings as settings_routes
from app.api.routes.auth import LoginLimiter
from app.bootstrap import create_model
from app.core.config import Settings, get_settings
from app.core.errors import DomainError
from app.ingestion.github_sync import create_read_client
from app.persistence.database import create_database

log = logging.getLogger("devflow")


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
    app.state.github_client_factory = create_read_client

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
        payload = {
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
        }
        if error.details is not None:
            payload["details"] = error.details
        return JSONResponse(
            status_code=error.status,
            content={"error": payload, "request_id": request.state.request_id},
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

    for module in (
        auth,
        health,
        settings_routes,
        repositories,
        sync,
        jobs,
        evidence,
        conversations,
        runs,
        documents,
        knowledge,
    ):
        app.include_router(module.router, prefix="/api/v1")

    return app


def app_factory():
    return create_app()
