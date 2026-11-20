from uuid import UUID

from fastapi import APIRouter, Depends, Request
from jsonschema.exceptions import SchemaError
from pydantic import ValidationError
from sqlalchemy import select

from app.api.deps import get_db, mutation_identity, repo_scope
from app.api.routes.platform import role, scoped, view
from app.core.errors import DomainError
from app.extensions.registry import InstallExtension, install
from app.persistence.models import ExtensionConfig
from app.schemas.responses import envelope
from app.services.memory_service import audit

router = APIRouter()


@router.get("/repos/{repo_id}/extensions")
def list_extensions(repo_scope_pair=Depends(repo_scope), db=Depends(get_db)):
    return envelope(
        [
            view(r)
            for r in db.scalars(
                select(ExtensionConfig)
                .where(ExtensionConfig.repo_id == repo_scope_pair[0].id)
                .order_by(ExtensionConfig.name, ExtensionConfig.version)
            )
        ]
    )


@router.post("/repos/{repo_id}/extensions", status_code=201)
def install_extension(
    body: InstallExtension,
    request: Request,
    repo_scope_pair=Depends(repo_scope),
    identity=Depends(mutation_identity),
    db=Depends(get_db),
):
    role(repo_scope_pair, {"admin"})
    try:
        return envelope(view(install(db, request.app.state.settings, repo_scope_pair[0], identity[0].id, body)))
    except (ValidationError, SchemaError):
        raise DomainError("INVALID_EXTENSION", "扩展配置或 Schema 不符合契约", 400) from None


@router.post("/repos/{repo_id}/extensions/{extension_id}/disable")
def disable_extension(
    extension_id: UUID,
    repo_scope_pair=Depends(repo_scope),
    identity=Depends(mutation_identity),
    db=Depends(get_db),
):
    role(repo_scope_pair, {"admin"})
    row = scoped(db, ExtensionConfig, extension_id, repo_scope_pair[0].id)
    row.state = "disabled"
    audit(db, row.repo_id, identity[0].id, row.id, "extension.disabled")
    db.commit()
    return envelope(view(row))
