"""Git workspace management (README 9.1).

A bare mirror per repository lives under var/workspaces/<repo_id>. Reads are
pinned to an explicit commit SHA; commits that are not yet mirrored are
fetched by SHA (GitHub serves PR head commits and branch tips this way).
Credentials travel via GIT_CONFIG_* environment variables so they never touch
command lines, URLs, .git/config or logs. Repository hooks, submodules, LFS
downloads and filters are never enabled.
"""

import base64
import logging
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from app.core.config import Settings
from app.core.errors import DomainError
from app.persistence.models import Repository

log = logging.getLogger("devflow.workspace")

CLONE_TIMEOUT_SECONDS = 300
FETCH_TIMEOUT_SECONDS = 120
LOCAL_TIMEOUT_SECONDS = 30


def default_remote_url(repo: Repository) -> str:
    return f"https://github.com/{repo.full_name}.git"


class WorkspaceManager:
    def __init__(
        self,
        settings: Settings,
        token: str = "",
        remote_url: Callable[[Repository], str] = default_remote_url,
    ):
        self.settings = settings
        self.token = token
        self.remote_url = remote_url
        self.proxy = settings.github_git_proxy
        self.root = Path(settings.storage_root) / "workspaces"

    def git_dir(self, repo_id) -> Path:
        return self.root / str(repo_id) / "git"

    def _auth_env(self) -> dict:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        entries: list[tuple[str, str]] = []
        if self.token:
            header = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
            entries.append(("http.extraheader", f"AUTHORIZATION: basic {header}"))
        if self.proxy:
            entries.append(("http.proxy", self.proxy))
            entries.append(("https.proxy", self.proxy))
        for index, (key, value) in enumerate(entries):
            env[f"GIT_CONFIG_KEY_{index}"] = key
            env[f"GIT_CONFIG_VALUE_{index}"] = value
        if entries:
            env["GIT_CONFIG_COUNT"] = str(len(entries))
        return env

    def _run(self, args: list[str], *, timeout: int, binary: bool = False):
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                timeout=timeout,
                env=self._auth_env(),
                check=False,
                **({} if binary else {"encoding": "utf-8", "errors": "replace"}),
            )
        except subprocess.TimeoutExpired:
            raise DomainError("CODE_UNAVAILABLE", "Git 操作超时", 502, retryable=True) from None
        except OSError:
            raise DomainError("CODE_UNAVAILABLE", "本机 Git 不可用", 502) from None
        return result

    def _git(self, git_dir: Path, args: list[str], *, timeout: int = LOCAL_TIMEOUT_SECONDS, binary=False):
        return self._run(["git", "--git-dir", str(git_dir), *args], timeout=timeout, binary=binary)

    def ensure_mirror(self, repo: Repository) -> Path:
        git_dir = self.git_dir(repo.id)
        if (git_dir / "HEAD").exists():
            result = self._git(git_dir, ["fetch", "--prune", "origin"], timeout=FETCH_TIMEOUT_SECONDS)
            if result.returncode != 0:
                raise DomainError(
                    "CODE_UNAVAILABLE", "无法更新代码镜像", 502, retryable=True
                )
            return git_dir
        git_dir.parent.mkdir(parents=True, exist_ok=True)
        result = self._run(
            ["git", "clone", "--mirror", self.remote_url(repo), str(git_dir)],
            timeout=CLONE_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            # Leave no half-cloned directory behind; the caller may retry.
            import shutil

            shutil.rmtree(git_dir, ignore_errors=True)
            log.info("clone failed for repo=%s", repo.id)
            raise DomainError("CODE_UNAVAILABLE", "无法克隆代码仓库", 502, retryable=True)
        return git_dir

    def commit_present(self, git_dir: Path, sha: str) -> bool:
        result = self._git(git_dir, ["cat-file", "-e", f"{sha}^{{commit}}"])
        return result.returncode == 0

    def ensure_commit(self, repo: Repository, sha: str) -> Path:
        if not sha or len(sha) < 7 or any(c not in "0123456789abcdef" for c in sha.lower()):
            raise DomainError("CODE_UNAVAILABLE", "提交标识不合法", 400)
        git_dir = self.ensure_mirror(repo)
        if self.commit_present(git_dir, sha):
            return git_dir
        result = self._git(
            git_dir, ["fetch", "origin", sha], timeout=FETCH_TIMEOUT_SECONDS
        )
        if result.returncode != 0 or not self.commit_present(git_dir, sha):
            raise DomainError(
                "CODE_UNAVAILABLE", "无法获取指定版本的代码", 502, retryable=True
            )
        return git_dir
