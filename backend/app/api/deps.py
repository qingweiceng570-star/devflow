import hmac

from fastapi import Depends, Request

from app.core.errors import DomainError
from app.services.auth_service import authenticate, digest


def get_db(request: Request):
    with request.app.state.session_factory() as session:
        yield session


def current_identity(request: Request, db=Depends(get_db)):
    return authenticate(db, request.cookies.get("devflow_session"))


def mutation_identity(request: Request, identity=Depends(current_identity)):
    user, session = identity
    token = request.headers.get("x-csrf-token", "")
    if not token or not hmac.compare_digest(digest(token), session.csrf_hash):
        raise DomainError("CSRF_FAILED", "请刷新登录状态后重试", 403)
    if user.role != "admin":
        raise DomainError("FORBIDDEN", "需要管理员权限", 403)
    return identity
