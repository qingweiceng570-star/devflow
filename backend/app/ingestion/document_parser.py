"""Document parsing for the knowledge base (README 13.1).

First version accepts UTF-8 Markdown and plain text only; anything else is
rejected explicitly instead of being stored as an empty husk.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings
from app.core.errors import DomainError

PARSER_VERSION = "plain-md.v1"
SUPPORTED_EXTENSIONS = {".md", ".markdown", ".txt"}


@dataclass
class ParsedDocument:
    text: str
    title: str
    content_hash: str
    parser_version: str = PARSER_VERSION


def parse_document(filename: str, content: bytes, settings: Settings) -> ParsedDocument:
    if len(content) > settings.max_document_bytes:
        raise DomainError(
            "PAYLOAD_TOO_LARGE",
            f"文档超过上限 {settings.max_document_bytes // (1024 * 1024)} MiB",
            413,
        )
    extension = Path(filename).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise DomainError("UNSUPPORTED_FORMAT", "仅支持 UTF-8 Markdown/纯文本（.md/.markdown/.txt）", 415)
    if b"\x00" in content[:8000]:
        raise DomainError("UNSUPPORTED_FORMAT", "二进制内容不支持", 415)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise DomainError("UNSUPPORTED_FORMAT", "仅支持 UTF-8 编码的文本", 415) from None
    if not text.strip():
        raise DomainError("INVALID_REQUEST", "文档内容为空", 400)
    title = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            break
        if stripped:
            title = stripped[:120]
            break
    title = title or Path(filename).stem
    return ParsedDocument(
        text=text,
        title=title[:300],
        content_hash=hashlib.sha256(content).hexdigest(),
    )
