import pytest
from argon2 import PasswordHasher
from sqlalchemy import select

from app.core.errors import DomainError
from app.persistence.models import Session, User
from app.services.auth_service import create_admin, login, set_admin_password


def test_password_policy_accepts_six_characters(database):
    with database() as db:
        create_admin(db, "six-char", "123456")
        user = db.scalar(select(User).where(User.username == "six-char"))
        assert user is not None
        assert PasswordHasher().verify(user.password_hash, "123456")


def test_password_policy_rejects_fewer_than_six_characters(database):
    with database() as db, pytest.raises(DomainError, match="6–128"):
        create_admin(db, "short", "12345")


def test_reset_password_revokes_existing_sessions(database):
    with database() as db:
        login(db, "admin", "test-only-password-123", 8)
        set_admin_password(db, "admin", "123456")
        assert db.scalar(select(Session)) is None
        with pytest.raises(DomainError):
            login(db, "admin", "test-only-password-123", 8)
        login(db, "admin", "123456", 8)
