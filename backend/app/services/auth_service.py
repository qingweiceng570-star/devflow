import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DBSession

from app.core.errors import DomainError
from app.persistence.models import Session, User

hasher = PasswordHasher()
DUMMY_HASH = hasher.hash(secrets.token_urlsafe(24))
MIN_PASSWORD_LENGTH = 6
MAX_PASSWORD_LENGTH = 128


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _validate_password(password: str) -> None:
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise DomainError(
            "WEAK_PASSWORD",
            f"密码长度需要为 {MIN_PASSWORD_LENGTH}–{MAX_PASSWORD_LENGTH} 个字符",
        )


def create_admin(db: DBSession, username: str, password: str) -> UUID:
    _validate_password(password)
    if db.scalar(select(User).where(User.username == username)):
        raise DomainError("USER_EXISTS", "账户已经存在", 409)
    user = User(username=username, password_hash=hasher.hash(password), role="admin")
    db.add(user)
    db.commit()
    return user.id


def set_admin_password(db: DBSession, username: str, password: str) -> None:
    """Replace an administrator password and revoke their existing sessions."""
    _validate_password(password)
    user = db.scalar(select(User).where(User.username == username))
    if not user:
        raise DomainError("USER_NOT_FOUND", "账户不存在", 404)
    user.password_hash = hasher.hash(password)
    db.execute(delete(Session).where(Session.user_id == user.id))
    db.commit()


def login(db: DBSession, username: str, password: str, hours: int):
    user = db.scalar(select(User).where(User.username == username))
    try:
        hasher.verify(user.password_hash if user else DUMMY_HASH, password)
    except (VerificationError, InvalidHashError):
        raise DomainError("INVALID_CREDENTIALS", "账户或密码不正确", 401) from None
    if not user or user.disabled:
        raise DomainError("INVALID_CREDENTIALS", "账户或密码不正确", 401)
    if hasher.check_needs_rehash(user.password_hash):
        user.password_hash = hasher.hash(password)
    db.execute(delete(Session).where(Session.expires_at <= datetime.now(UTC)))
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    session = Session(
        user_id=user.id,
        token_hash=digest(token),
        csrf_hash=digest(csrf),
        expires_at=datetime.now(UTC) + timedelta(hours=hours),
    )
    db.add(session)
    db.commit()
    return user, token, csrf


def authenticate(db: DBSession, token: str | None):
    if not token or len(token) > 100:
        raise DomainError("UNAUTHENTICATED", "请先登录", 401)
    session = db.scalar(select(Session).where(Session.token_hash == digest(token)))
    if not session or as_utc(session.expires_at) <= datetime.now(UTC):
        raise DomainError("UNAUTHENTICATED", "登录已过期，请重新登录", 401)
    user = db.get(User, session.user_id)
    if not user or user.disabled:
        raise DomainError("UNAUTHENTICATED", "账户不可用", 401)
    return user, session
