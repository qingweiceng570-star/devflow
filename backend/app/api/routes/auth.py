import threading
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import delete

from app.api.deps import current_identity, get_db, mutation_identity
from app.core.errors import DomainError
from app.persistence.models import Session
from app.schemas.requests import LoginRequest
from app.schemas.responses import envelope
from app.services.auth_service import login

router = APIRouter()


class LoginLimiter:
    """Single-API-process M0 limiter; persistent distributed limiting is a later stage.

    Only failed credential checks consume the budget, so automated checks and
    normal re-logins are not locked out by repeated successful sign-ins.
    """

    def __init__(self):
        self.attempts = defaultdict(deque)
        self.lock = threading.Lock()

    def _prune(self, key):
        now = time.monotonic()
        for stale_key in list(self.attempts):
            queue = self.attempts[stale_key]
            while queue and queue[0] <= now - 60:
                queue.popleft()
            if not queue:
                del self.attempts[stale_key]
        return self.attempts[key]

    def check(self, key):
        with self.lock:
            if len(self._prune(key)) >= 10:
                raise DomainError(
                    "RATE_LIMITED", "登录尝试过多，请一分钟后重试", 429, retryable=True
                )

    def failure(self, key):
        with self.lock:
            self._prune(key).append(time.monotonic())


def user_data(user):
    return {"id": str(user.id), "username": user.username, "role": user.role}


@router.post("/auth/login")
def login_route(body: LoginRequest, request: Request, response: Response, db=Depends(get_db)):
    settings = request.app.state.settings
    client = request.client.host if request.client else "unknown"
    limiter = request.app.state.login_limiter
    limiter.check(client)
    try:
        user, token, csrf = login(db, body.username, body.password, settings.session_hours)
    except DomainError as error:
        if error.code == "INVALID_CREDENTIALS":
            limiter.failure(client)
        raise
    cookie_options = dict(
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
        max_age=settings.session_hours * 3600,
    )
    response.set_cookie("devflow_session", token, httponly=True, **cookie_options)
    response.set_cookie("devflow_csrf", csrf, httponly=False, **cookie_options)
    return envelope(user_data(user))


@router.get("/auth/me")
def me(identity=Depends(current_identity)):
    return envelope(user_data(identity[0]))


@router.post("/auth/logout", status_code=204)
def logout(response: Response, identity=Depends(mutation_identity), db=Depends(get_db)):
    db.execute(delete(Session).where(Session.id == identity[1].id))
    db.commit()
    response.delete_cookie("devflow_session", path="/")
    response.delete_cookie("devflow_csrf", path="/")
