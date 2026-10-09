from functools import lru_cache

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Depends
from sqlalchemy import text

from app.api.deps import get_db
from app.core.config import PROJECT_ROOT
from app.core.errors import DomainError
from app.schemas.responses import envelope

router = APIRouter()


@lru_cache
def _head_revisions() -> frozenset[str]:
    """Derive the expected schema revision from the migration files themselves
    so a new milestone cannot leave this check behind."""
    config = Config(str(PROJECT_ROOT / "backend" / "alembic.ini"))
    return frozenset(ScriptDirectory.from_config(config).get_heads())


@router.get("/health/live")
def live():
    return envelope({"status": "alive", "version": "0.1.0"})


@router.get("/health/ready")
def ready(db=Depends(get_db)):
    try:
        revision = db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        if revision not in _head_revisions():
            raise ValueError("Schema mismatch")
    except Exception:
        raise DomainError(
            "DATABASE_NOT_READY", "数据库不可用或尚未执行迁移", 503
        ) from None
    return envelope({"status": "ready", "schema_revision": revision})
