import re
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.core.config import Settings
from app.core.errors import DomainError
from app.integrations.github import normalizers
from app.integrations.github.client import GitHubClient
from app.integrations.github.credentials import CREDENTIAL_REFS, resolve_read_token
from app.persistence.models import RepoMembership, Repository, User

FULL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")


def register_repo(
    db: DBSession,
    settings: Settings,
    user: User,
    full_name: str,
    credential_ref: str,
    client_factory=None,
) -> tuple[Repository, bool]:
    """Register or refresh one repository. Deduplication is by GitHub external
    ID, so a renamed repository updates full_name instead of creating a row."""
    if not FULL_NAME_RE.fullmatch(full_name):
        raise DomainError("INVALID_REQUEST", "仓库格式应为 owner/repo", 400)
    if credential_ref not in CREDENTIAL_REFS:
        raise DomainError("CREDENTIAL_UNKNOWN", "凭据引用未配置", 400)
    maker = client_factory or (lambda s, t: GitHubClient(s, t))
    token = resolve_read_token(settings, credential_ref)
    raw = maker(settings, token).get(f"/repos/{full_name}").data
    data = normalizers.normalize_repo(raw)

    repo = db.scalar(
        select(Repository).where(
            Repository.provider == "github", Repository.external_id == data["external_id"]
        )
    )
    created = repo is None
    now = datetime.now(UTC)
    if repo is None:
        repo = Repository(
            provider="github",
            external_id=data["external_id"],
            full_name=data["full_name"],
            credential_ref=credential_ref,
            default_branch=data["default_branch"],
        )
        db.add(repo)
        db.flush()
    else:
        if repo.full_name != data["full_name"]:
            repo.full_name = data["full_name"]
            repo.version += 1
        repo.credential_ref = credential_ref
        repo.default_branch = data["default_branch"]
        repo.updated_at = now

    membership = db.scalar(
        select(RepoMembership).where(
            RepoMembership.user_id == user.id, RepoMembership.repo_id == repo.id
        )
    )
    if membership is None:
        db.add(RepoMembership(user_id=user.id, repo_id=repo.id, role="admin"))
    db.commit()
    return repo, created


def visible_repos(db: DBSession, user: User, cursor=None, limit: int = 20):
    from app.persistence.models import RepoMembership as _M  # local alias for readability

    query = (
        select(Repository)
        .join(_M, _M.repo_id == Repository.id)
        .where(_M.user_id == user.id)
        .order_by(Repository.created_at.desc(), Repository.id.desc())
    )
    if cursor is not None:
        created_at, row_id = cursor
        query = query.where(
            (Repository.created_at < created_at)
            | ((Repository.created_at == created_at) & (Repository.id < row_id))
        )
    rows = db.scalars(query.limit(limit + 1)).all()
    return rows[:limit], (rows[limit] if len(rows) > limit else None)


def require_repo(db: DBSession, user: User, repo_id: UUID) -> tuple[Repository, RepoMembership]:
    """Fetch a repository the user belongs to; 404 otherwise (never reveal
    whether an unauthorized repository exists)."""
    membership = db.scalar(
        select(RepoMembership).where(
            RepoMembership.user_id == user.id, RepoMembership.repo_id == repo_id
        )
    )
    repo = db.get(Repository, repo_id) if membership else None
    if repo is None or membership is None:
        raise DomainError("RESOURCE_NOT_FOUND", "资源不存在", 404)
    return repo, membership
