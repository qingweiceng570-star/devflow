from fastapi import APIRouter, Depends
from sqlalchemy import text

from app.api.deps import get_db
from app.core.errors import DomainError
from app.schemas.responses import envelope

router = APIRouter()

EXPECTED_REVISIONS = {"0002_m1"}


@router.get("/health/live")
def live():
    return envelope({"status": "alive", "version": "0.1.0"})


@router.get("/health/ready")
def ready(db=Depends(get_db)):
    try:
        revision = db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        if revision not in EXPECTED_REVISIONS:
            raise ValueError("Schema mismatch")
    except Exception:
        raise DomainError(
            "DATABASE_NOT_READY", "数据库不可用或尚未执行迁移", 503
        ) from None
    return envelope({"status": "ready", "schema_revision": revision})
