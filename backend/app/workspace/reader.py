"""Pinned-SHA file reads with explicit bounds (README 9.2 read_file)."""

from dataclasses import dataclass

from app.core.config import Settings
from app.core.errors import DomainError
from app.persistence.models import Repository
from app.workspace.manager import WorkspaceManager
from app.workspace.path_policy import validate_relative_path

_BINARY_SNIFF_BYTES = 8000


@dataclass
class FileSlice:
    path: str
    commit_sha: str
    line_start: int
    line_end: int
    total_lines: int
    text: str
    truncated: bool


def _object_spec(sha: str, path: str) -> str:
    return f"{sha}:{path}"


def read_file(
    settings: Settings,
    manager: WorkspaceManager,
    repo: Repository,
    sha: str,
    path: str,
    start_line: int = 1,
    end_line: int | None = None,
) -> FileSlice:
    path = validate_relative_path(path)
    if start_line < 1:
        raise DomainError("INVALID_REQUEST", "起始行号必须不小于 1", 400)
    git_dir = manager.ensure_commit(repo, sha)

    size_result = manager._git(git_dir, ["cat-file", "-s", _object_spec(sha, path)])
    if size_result.returncode != 0:
        raise DomainError("CODE_PATH_NOT_FOUND", "指定版本中不存在该文件", 404)
    try:
        size = int(size_result.stdout.strip())
    except ValueError:
        raise DomainError("CODE_UNAVAILABLE", "无法读取文件大小", 502) from None
    if size > settings.max_code_file_bytes:
        raise DomainError("CODE_FILE_TOO_LARGE", "文件超过读取上限", 400)

    content_result = manager._git(
        git_dir, ["cat-file", "-p", _object_spec(sha, path)], binary=True
    )
    if content_result.returncode != 0:
        raise DomainError("CODE_PATH_NOT_FOUND", "指定版本中不存在该文件", 404)
    raw: bytes = content_result.stdout
    if b"\x00" in raw[:_BINARY_SNIFF_BYTES]:
        raise DomainError("CODE_BINARY_UNSUPPORTED", "二进制文件不支持读取", 400)

    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    total = len(lines)
    if total == 0 or start_line > total:
        raise DomainError("CODE_LINE_RANGE", "行号超出文件范围", 400)
    stop = min(end_line or (start_line + settings.max_read_lines - 1), start_line + settings.max_read_lines - 1, total)
    if stop < start_line:
        raise DomainError("CODE_LINE_RANGE", "结束行号小于起始行号", 400)
    return FileSlice(
        path=path,
        commit_sha=sha,
        line_start=start_line,
        line_end=stop,
        total_lines=total,
        text="\n".join(lines[start_line - 1 : stop]),
        truncated=stop < total,
    )
