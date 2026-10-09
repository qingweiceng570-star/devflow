"""Repository-relative path policy (README 9.2).

Paths are treated as Git names (forward slashes, case-sensitive). Anything
that could escape the repository or alias a different file on Windows is
rejected: absolute paths, drive letters, UNC prefixes, ".." segments, NUL,
NTFS alternate data streams, and segments that Windows would trim or fold.
"""

from app.core.errors import DomainError

MAX_PATH_LENGTH = 1024


def validate_relative_path(path: str) -> str:
    if not path or len(path) > MAX_PATH_LENGTH:
        raise DomainError("PATH_REJECTED", "路径为空或过长", 400)
    if "\x00" in path:
        raise DomainError("PATH_REJECTED", "路径包含非法字符", 400)
    if "\\" in path:
        raise DomainError("PATH_REJECTED", "路径必须使用正斜杠", 400)
    if path.startswith("/"):
        raise DomainError("PATH_REJECTED", "不允许绝对路径", 400)
    if len(path) >= 2 and path[1] == ":":
        raise DomainError("PATH_REJECTED", "不允许盘符路径", 400)
    segments = path.split("/")
    for segment in segments:
        if segment in {"", ".", ".."}:
            raise DomainError("PATH_REJECTED", "路径包含越界片段", 400)
        if ":" in segment:
            raise DomainError("PATH_REJECTED", "路径包含非法字符", 400)
        if segment.endswith(".") or segment.endswith(" "):
            raise DomainError("PATH_REJECTED", "路径片段以点或空格结尾", 400)
        if any(ord(char) < 0x20 for char in segment):
            raise DomainError("PATH_REJECTED", "路径包含控制字符", 400)
    return "/".join(segments)


def validate_path_glob(path_glob: str | None) -> str | None:
    """Pathspec for search_code; supports * and ? within Git-style paths."""
    if path_glob is None:
        return None
    if not path_glob or len(path_glob) > MAX_PATH_LENGTH:
        raise DomainError("PATH_REJECTED", "路径模式为空或过长", 400)
    if "\x00" in path_glob or "\\" in path_glob or ":" in path_glob:
        raise DomainError("PATH_REJECTED", "路径模式包含非法字符", 400)
    if path_glob.startswith("/") or ".." in path_glob.split("/"):
        raise DomainError("PATH_REJECTED", "路径模式不允许越界", 400)
    return path_glob
