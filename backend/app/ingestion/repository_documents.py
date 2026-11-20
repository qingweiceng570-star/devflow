"""Read documentation from one immutable Git tree without checking files out."""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from app.core.config import Settings
from app.core.errors import DomainError
from app.ingestion.document_parser import SUPPORTED_EXTENSIONS, parse_document
from app.persistence.models import Repository
from app.workspace.manager import WorkspaceManager
from app.workspace.path_policy import validate_relative_path

MAX_DOCUMENTS = 200
MAX_TOTAL_BYTES = 10 * 1024 * 1024
MAX_FILE_BYTES = 1024 * 1024
MAX_TREE_BYTES = 32 * 1024 * 1024
DOC_DIRECTORIES = {"docs", "doc", "documentation"}
EXCLUDED_DIRECTORIES = {"node_modules", "vendor", "dist", "build", "site", "_build"}


@dataclass
class RepositoryDocument:
    path: str
    content: bytes
    parser_filename: str


@dataclass
class DocumentSnapshot:
    commit_sha: str
    documents: list[RepositoryDocument] = field(default_factory=list)
    present_paths: set[str] = field(default_factory=set)
    discovered: int = 0
    skipped: list[dict] = field(default_factory=list)


def is_document_path(path: str) -> bool:
    candidate = PurePosixPath(path)
    parents = [part.lower() for part in candidate.parts[:-1]]
    if any(part.startswith(".") or part in EXCLUDED_DIRECTORIES for part in parents):
        return False
    name = candidate.name.lower()
    if name.startswith("."):
        return False
    supported = candidate.suffix.lower() in SUPPORTED_EXTENSIONS
    readme = name == "readme" or (name.startswith("readme.") and supported)
    return readme or (supported and (not parents or bool(DOC_DIRECTORIES.intersection(parents))))


def read_document_snapshot(
    settings: Settings,
    manager: WorkspaceManager,
    repo: Repository,
    *,
    check_stop: Callable[[], None],
) -> DocumentSnapshot:
    check_stop()
    git_dir = manager.ensure_mirror(repo)
    check_stop()
    ref = f"refs/heads/{repo.default_branch}" if repo.default_branch else "HEAD"
    head = manager._git(git_dir, ["rev-parse", "--verify", f"{ref}^{{commit}}"])
    sha = head.stdout.strip()
    if head.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", sha):
        raise DomainError("DOCUMENT_IMPORT_FAILED", "无法读取默认分支，请确认仓库已有提交", 502)
    check_stop()
    tree = manager._git(git_dir, ["ls-tree", "-r", "-l", "-z", sha], binary=True)
    if tree.returncode != 0:
        raise DomainError("DOCUMENT_IMPORT_FAILED", "无法读取仓库文档列表", 502, retryable=True)
    if len(tree.stdout) > MAX_TREE_BYTES:
        raise DomainError("DOCUMENT_IMPORT_LIMIT", "仓库文件列表过大，暂不支持自动导入", 413)
    snapshot = DocumentSnapshot(commit_sha=sha)
    candidates = []
    for entry in tree.stdout.split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        try:
            path = validate_relative_path(raw_path.decode("utf-8"))
        except (UnicodeDecodeError, DomainError):
            continue
        snapshot.present_paths.add(path)
        if not is_document_path(path):
            continue
        snapshot.discovered += 1
        mode, kind, oid, size = metadata.split()
        if mode not in {b"100644", b"100755"} or kind != b"blob":
            snapshot.skipped.append({"path": path, "reason": "非普通文件"})
            continue
        if len("git:" + path) > 300:
            snapshot.skipped.append({"path": path, "reason": "路径过长"})
            continue
        candidates.append((path, oid.decode("ascii"), int(size)))
    # Root README first, then root guides and documentation directories.
    candidates.sort(key=lambda item: (not item[0].lower().startswith("readme"), item[0].count("/"), item[0]))
    total_bytes = 0
    for path, oid, size in candidates:
        reason = None
        if size > min(MAX_FILE_BYTES, settings.max_document_bytes):
            reason = "超过单文件大小上限"
        elif len(snapshot.documents) >= MAX_DOCUMENTS or total_bytes + size > MAX_TOTAL_BYTES:
            reason = "达到本次导入数量或总大小上限"
        if reason:
            snapshot.skipped.append({"path": path, "reason": reason})
            continue
        check_stop()
        blob = manager._git(git_dir, ["cat-file", "blob", oid], binary=True)
        if blob.returncode != 0:
            raise DomainError("DOCUMENT_IMPORT_FAILED", "读取仓库文档失败，请重试", 502, retryable=True)
        filename = path if PurePosixPath(path).suffix else path + ".txt"
        try:
            parse_document(filename, blob.stdout, settings)
        except DomainError as error:
            snapshot.skipped.append({"path": path, "reason": error.message})
            continue
        snapshot.documents.append(RepositoryDocument(path, blob.stdout, filename))
        total_bytes += len(blob.stdout)
    return snapshot
