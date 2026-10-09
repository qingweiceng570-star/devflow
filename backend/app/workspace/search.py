"""Fixed-string code search at a pinned SHA (README 9.2 search_code).

First version accepts literal strings only - no regular expressions. Output
uses git grep's NUL-separated format, which keeps paths with unusual
characters unambiguous. Ordering is git's tree order (deterministic).
"""

import base64
import binascii
from dataclasses import dataclass, field

from app.core.config import Settings
from app.core.errors import DomainError
from app.persistence.models import Repository
from app.workspace.manager import WorkspaceManager
from app.workspace.path_policy import validate_path_glob

MAX_QUERY_LENGTH = 256


@dataclass
class SearchHit:
    path: str
    line_number: int
    line: str


@dataclass
class SearchResults:
    hits: list[SearchHit] = field(default_factory=list)
    next_cursor: str | None = None
    truncated: bool = False
    total_matches: int = 0


def _decode_offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        prefix, _, value = raw.partition(":")
        if prefix != "grep":
            raise ValueError
        return max(0, int(value))
    except (ValueError, binascii.Error):
        raise DomainError("INVALID_REQUEST", "分页游标不合法", 400) from None


def _encode_offset(offset: int) -> str:
    return base64.urlsafe_b64encode(f"grep:{offset}".encode()).decode()


def parse_grep_z(output: bytes) -> list[SearchHit]:
    """Parse `git grep -z -n` output.

    Records are `<rev>:<path>\\0<line>\\0<content>\\n` sequences: header and
    line number are NUL-terminated, the match line is newline-terminated
    (a match line never contains an embedded newline).
    """
    hits: list[SearchHit] = []
    pos = 0
    while pos < len(output):
        nul1 = output.find(b"\x00", pos)
        if nul1 == -1:
            break
        header = output[pos:nul1]
        nul2 = output.find(b"\x00", nul1 + 1)
        if nul2 == -1:
            break
        line_raw = output[nul1 + 1 : nul2]
        newline = output.find(b"\n", nul2 + 1)
        if newline == -1:
            newline = len(output)
        content = output[nul2 + 1 : newline]
        path_part = header.split(b":", 1)[1] if b":" in header else b""
        try:
            hits.append(
                SearchHit(
                    path=path_part.decode("utf-8", errors="replace"),
                    line_number=int(line_raw),
                    line=content.decode("utf-8", errors="replace"),
                )
            )
        except ValueError:
            break
        pos = newline + 1
    return hits


def search_code(
    settings: Settings,
    manager: WorkspaceManager,
    repo: Repository,
    sha: str,
    query: str,
    path_glob: str | None = None,
    cursor: str | None = None,
) -> SearchResults:
    if not query or len(query) > MAX_QUERY_LENGTH:
        raise DomainError("INVALID_REQUEST", "查询为空或过长", 400)
    if "\x00" in query:
        raise DomainError("INVALID_REQUEST", "查询包含非法字符", 400)
    glob_validated = validate_path_glob(path_glob)
    offset = _decode_offset(cursor)
    git_dir = manager.ensure_commit(repo, sha)

    args = ["grep", "-n", "-F", "-I", "-z", "-e", query, sha]
    if glob_validated:
        args += ["--", glob_validated]
    result = manager._git(git_dir, args, timeout=int(settings.search_timeout_seconds), binary=True)
    if result.returncode not in (0, 1):
        raise DomainError("CODE_SEARCH_FAILED", "代码搜索执行失败", 502)
    hits = parse_grep_z(result.stdout or b"")
    total = len(hits)
    window = hits[offset : offset + settings.max_search_matches]
    next_cursor = None
    truncated = False
    if total > offset + settings.max_search_matches:
        next_cursor = _encode_offset(offset + settings.max_search_matches)
        truncated = True
    return SearchResults(hits=window, next_cursor=next_cursor, truncated=truncated, total_matches=total)
