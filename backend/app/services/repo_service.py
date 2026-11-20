import base64
import os
import re
import subprocess
from datetime import UTC, datetime
from urllib.parse import unquote, urlsplit
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from app.core.config import Settings
from app.core.errors import DomainError
from app.integrations.github import normalizers
from app.integrations.github.client import GitHubClient, SourceRateLimited
from app.integrations.github.credentials import CREDENTIAL_REFS, resolve_read_token
from app.persistence.models import RepoMembership, Repository, User

FULL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
GITHUB_HOSTS = {"github.com", "www.github.com"}
CLONE_PREFIX_RE = re.compile(r"^git\s+clone\s+", re.IGNORECASE)
# ``git@github.com:owner/repo`` and ``ssh://git@github.com/owner/repo`` are
# the two clone forms GitHub shows on the repository page.
SCP_PREFIX_RE = re.compile(r"^(?:ssh://)?git@github\.com(?::\d{1,5})?[:/]", re.IGNORECASE)
BARE_HOST_RE = re.compile(r"^(?:www\.)?github\.com/+", re.IGNORECASE)
# Text pasted from chats and documents often keeps trailing punctuation or a
# wrapping quote; none of these characters can be part of a repository name.
TRAILING_NOISE = "。，、；：！？）】》”’\")]}>'\"`,;"
# A link pasted from an article often ends with a short parenthetical comment.
TRAILING_COMMENT_RE = re.compile(r"\s*[（(][^（）()]{0,60}[）)]\s*$")

EMPTY_MESSAGE = "请输入 GitHub 仓库链接，例如 https://github.com/owner/repo"
HOST_MESSAGE = "仅支持 github.com 仓库链接，请检查链接域名"
INCOMPLETE_MESSAGE = "请提供完整的 GitHub 仓库链接，例如 https://github.com/owner/repo"
FORMAT_MESSAGE = "GitHub 仓库格式不正确，请使用 owner/repo 或仓库链接"

# Provisional identity used when the repository is verified over the git
# protocol because the REST API quota is exhausted. The numeric API id
# replaces it as soon as an API call succeeds.
GIT_ID_PREFIX = "git:"
GIT_PROBE_TIMEOUT_SECONDS = 15


def _git_probe(settings: Settings, token: str, full_name: str) -> tuple[bool, str | None]:
    """Verify a repository with ``git ls-remote`` and return its default branch.

    ``git ls-remote`` uses the git protocol, which is not governed by the
    REST API's anonymous hourly quota.  It is only used as a fallback when the
    API reports ``SourceRateLimited`` (common behind a shared proxy IP).
    """
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    entries: list[tuple[str, str]] = []
    if token:
        header = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        entries.append(("http.extraheader", f"AUTHORIZATION: basic {header}"))
    if settings.github_git_proxy:
        entries.append(("http.proxy", settings.github_git_proxy))
        entries.append(("https.proxy", settings.github_git_proxy))
    for index, (key, value) in enumerate(entries):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    if entries:
        env["GIT_CONFIG_COUNT"] = str(len(entries))
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--symref", f"https://github.com/{full_name}.git", "HEAD"],
            capture_output=True,
            timeout=GIT_PROBE_TIMEOUT_SECONDS,
            env=env,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, None
    if result.returncode != 0:
        return False, None
    for line in result.stdout.splitlines():
        if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
            return True, line[len("ref: refs/heads/") : -len("\tHEAD")]
    # An existing but empty repository has no refs yet.
    return True, None


def normalize_repo_input(value: str) -> str:
    """Convert common GitHub repository inputs to a canonical ``owner/repo``.

    The import endpoint is intentionally limited to github.com.  We accept a
    repository page, a link copied from a repository sub-page, the short
    ``github.com/owner/repo`` form, an HTTPS/SSH ``git clone`` command and the
    usual ``git@github.com:owner/repo`` clone address.  Query strings,
    fragments, wrapping quotes and a trailing ``.git`` are presentation
    details and do not change the repository identity.
    """
    raw = (value or "").strip()
    raw = raw.strip("'\"`“”‘’").strip()
    raw = CLONE_PREFIX_RE.sub("", raw, count=1).strip()
    raw = TRAILING_COMMENT_RE.sub("", raw).strip()
    raw = raw.rstrip(TRAILING_NOISE).strip()
    if not raw:
        raise DomainError("INVALID_REQUEST", EMPTY_MESSAGE, 400)

    if SCP_PREFIX_RE.match(raw):
        path = SCP_PREFIX_RE.sub("", raw, count=1)
    elif "://" in raw:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower().rstrip(".")
        try:
            port = parsed.port
        except ValueError:
            raise DomainError("INVALID_REQUEST", FORMAT_MESSAGE, 400) from None
        if scheme in {"http", "https"}:
            if host not in GITHUB_HOSTS:
                raise DomainError("INVALID_REQUEST", HOST_MESSAGE, 400)
            if parsed.password or (parsed.username or "") not in {"", "git"}:
                raise DomainError("INVALID_REQUEST", FORMAT_MESSAGE, 400)
            if port not in {None, 80, 443}:
                raise DomainError("INVALID_REQUEST", FORMAT_MESSAGE, 400)
        elif scheme in {"ssh", "git"}:
            if host != "github.com":
                raise DomainError("INVALID_REQUEST", HOST_MESSAGE, 400)
            if parsed.password or (parsed.username or "") not in {"", "git"}:
                raise DomainError("INVALID_REQUEST", FORMAT_MESSAGE, 400)
            if port not in {None, 22}:
                raise DomainError("INVALID_REQUEST", FORMAT_MESSAGE, 400)
        else:
            raise DomainError("INVALID_REQUEST", HOST_MESSAGE, 400)
        path = parsed.path
    elif BARE_HOST_RE.match(raw):
        path = BARE_HOST_RE.sub("", raw, count=1)
    else:
        path = raw

    path = unquote(path).strip().strip("/")
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) < 2:
        raise DomainError("INVALID_REQUEST", INCOMPLETE_MESSAGE, 400)
    owner, repository = segments[0], segments[1]
    if repository.lower().endswith(".git") and len(repository) > 4:
        repository = repository[:-4]
    full_name = f"{owner}/{repository}"
    if not FULL_NAME_RE.fullmatch(full_name):
        raise DomainError("INVALID_REQUEST", FORMAT_MESSAGE, 400)
    return full_name


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
    full_name = normalize_repo_input(full_name)
    if credential_ref not in CREDENTIAL_REFS:
        raise DomainError("CREDENTIAL_UNKNOWN", "凭据引用未配置", 400)
    maker = client_factory or (lambda s, t: GitHubClient(s, t))
    token = resolve_read_token(settings, credential_ref)
    try:
        raw = maker(settings, token).get(f"/repos/{full_name}").data
        data = normalizers.normalize_repo(raw)
    except SourceRateLimited:
        # The anonymous REST quota is per egress IP and is easily exhausted
        # behind a shared proxy.  Import can still be verified over the git
        # protocol, which does not consume that quota.
        exists, branch = _git_probe(settings, token, full_name)
        if not exists:
            raise DomainError(
                "GITHUB_RATE_LIMITED",
                "GitHub 接口额度已用尽，且无法通过 git 校验该仓库；"
                "请确认链接、网络或代理可用，或配置 GITHUB_READ_TOKEN 后重试",
                429,
            ) from None
        data = {
            "external_id": f"{GIT_ID_PREFIX}{full_name.lower()}",
            "full_name": full_name,
            "default_branch": branch,
        }

    # The column is bounded; GitHub branch names may be longer than we store.
    if data.get("default_branch") and len(data["default_branch"]) > 200:
        data["default_branch"] = data["default_branch"][:200]

    repo = db.scalar(
        select(Repository).where(
            Repository.provider == "github", Repository.external_id == data["external_id"]
        )
    )
    if repo is None:
        # A repository imported while the API was rate limited carries a
        # provisional git id. Reuse that row instead of creating a duplicate,
        # and upgrade its identity once the numeric API id is available.
        same_name = db.scalar(
            select(Repository).where(
                Repository.provider == "github",
                func.lower(Repository.full_name) == full_name.lower(),
            )
        )
        if same_name is not None:
            repo = same_name
            if same_name.external_id.startswith(GIT_ID_PREFIX):
                if not data["external_id"].startswith(GIT_ID_PREFIX):
                    same_name.external_id = data["external_id"]
            else:
                data["external_id"] = same_name.external_id
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
        if data["default_branch"] is not None:
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


def require_repo(db: DBSession, user: User, repo_id: UUID, minimum_role="viewer") -> tuple[Repository, RepoMembership]:
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
    levels = {"viewer": 0, "analyst": 1, "maintainer": 2, "admin": 3}
    if levels.get(membership.role, -1) < levels[minimum_role]:
        raise DomainError("FORBIDDEN", "当前仓库角色没有此权限", 403)
    return repo, membership
