import hashlib
import os
import re
import tempfile
from pathlib import Path
from uuid import UUID

from app.core.errors import DomainError

_OBJECT_REF = re.compile(r"^objects/[0-9a-f-]{36}/[a-z_]+/[0-9a-f]{64}$")
_CATEGORY = re.compile(r"^[a-z_]+$")


def put_object(storage_root: Path, repo_id: UUID, category: str, content: bytes) -> tuple[str, str]:
    """Atomically store content and return (object_ref, sha256).

    The reference is derived from the content hash, so identical content is
    stored once. Nothing is visible under its final name until fully written.
    """
    if not _CATEGORY.fullmatch(category):
        raise ValueError("Invalid object category")
    digest = hashlib.sha256(content).hexdigest()
    object_ref = f"objects/{repo_id}/{category}/{digest}"
    target = storage_root / "objects" / str(repo_id) / category / digest
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        handle_fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=target.parent)
        try:
            with os.fdopen(handle_fd, "wb") as handle:
                handle.write(content)
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    return object_ref, digest


def read_object(storage_root: Path, object_ref: str) -> bytes:
    if not _OBJECT_REF.fullmatch(object_ref):
        raise DomainError("OBJECT_INVALID", "对象引用不合法", 500)
    return (storage_root / object_ref).read_bytes()


def text_excerpt(text: str | None, limit: int = 500) -> tuple[str, bool]:
    """First characters of a text body plus a truncation flag."""
    if not text:
        return "", False
    collapsed = text.strip()
    if len(collapsed) <= limit:
        return collapsed, False
    return collapsed[:limit], True
