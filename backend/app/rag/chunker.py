"""Structure-aware document chunking (README 13.1).

Markdown/plain text is parsed into heading, paragraph, list and fenced-code
blocks; chunks follow the structure (never a fixed character window), keep the
heading path and original line ranges, target ~600 tokens with a 900 maximum,
and carry at most ~80 tokens of overlap between neighbouring prose chunks.
Long code blocks split on line boundaries and inherit their heading.
"""

import hashlib
import re
from dataclasses import dataclass, field

from app.context.budgeter import ESTIMATOR_VERSION, estimate_tokens
from app.core.config import Settings

CHUNKER_VERSION = "structure.v1"

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?;；])\s*")


@dataclass
class Chunk:
    ordinal: int
    text: str
    heading_path: list[str] = field(default_factory=list)
    line_start: int = 1
    line_end: int = 1
    token_estimate: int = 0


@dataclass
class _Block:
    kind: str  # heading | paragraph | list | code
    text: str
    line_start: int
    line_end: int
    level: int = 0  # heading level


def parse_blocks(text: str) -> list[_Block]:
    lines = text.splitlines()
    blocks: list[_Block] = []
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        match = _HEADING.match(line)
        if match:
            blocks.append(
                _Block("heading", match.group(2).strip(), index + 1, index + 1, len(match.group(1)))
            )
            index += 1
            continue
        if line.strip().startswith("```"):
            start = index
            index += 1
            while index < total and not lines[index].strip().startswith("```"):
                index += 1
            end = min(index, total - 1)
            blocks.append(_Block("code", "\n".join(lines[start : end + 1]), start + 1, end + 1))
            index = end + 1
            continue
        if not line.strip():
            index += 1
            continue
        kind = "list" if _LIST_ITEM.match(line) else "paragraph"
        start = index
        while index < total and lines[index].strip():
            if _HEADING.match(lines[index]) or lines[index].strip().startswith("```"):
                break
            if kind == "paragraph" and _LIST_ITEM.match(lines[index]) and index != start:
                break
            index += 1
        blocks.append(_Block(kind, "\n".join(lines[start:index]), start + 1, index))
    return blocks


def _split_oversized(block: _Block, max_tokens: int) -> list[_Block]:
    if estimate_tokens(block.text) <= max_tokens:
        return [block]
    pieces: list[_Block] = []
    lines = block.text.splitlines()
    buffer: list[str] = []
    buffer_start = block.line_start
    for offset, line in enumerate(lines):
        line_number = block.line_start + offset
        candidate = "\n".join([*buffer, line])
        if buffer and estimate_tokens(candidate) > max_tokens:
            pieces.append(_Block(block.kind, "\n".join(buffer), buffer_start, line_number - 1))
            buffer = [line]
            buffer_start = line_number
        elif not buffer and estimate_tokens(line) > max_tokens:
            # A single pathological line: cut on sentence boundaries, then hard.
            chunk: list[str] = []
            for sentence in _SENTENCE_SPLIT.split(line):
                guess = "".join([*chunk, sentence])
                if chunk and estimate_tokens(guess) > max_tokens:
                    pieces.append(_Block(block.kind, "".join(chunk), line_number, line_number))
                    chunk = [sentence]
                else:
                    chunk.append(sentence)
            if chunk:
                pieces.append(_Block(block.kind, "".join(chunk), line_number, line_number))
            buffer = []
            buffer_start = line_number + 1
        else:
            buffer.append(line)
    if buffer:
        pieces.append(_Block(block.kind, "\n".join(buffer), buffer_start, block.line_end))
    return pieces


def chunk_document(text: str, settings: Settings) -> list[Chunk]:
    target = settings.rag_chunk_target_tokens
    maximum = max(settings.rag_chunk_max_tokens, target)
    overlap = settings.rag_chunk_overlap_tokens

    chunks: list[Chunk] = []
    heading_stack: list[tuple[int, str]] = []
    pending_heading_line: str | None = None
    current_lines: list[str] = []
    current_tokens = 0
    current_start: int | None = None
    current_end: int | None = None
    current_heading: list[str] = []
    overlap_lines: list[str] = []

    def flush() -> None:
        nonlocal current_lines, current_tokens, current_start, current_end, current_heading
        if not current_lines:
            return
        body = "\n".join(current_lines)
        chunks.append(
            Chunk(
                ordinal=len(chunks),
                text=body,
                heading_path=list(current_heading),
                line_start=current_start or 1,
                line_end=current_end or current_start or 1,
                token_estimate=estimate_tokens(body),
            )
        )
        if overlap > 0 and current_lines:
            tail: list[str] = []
            for line in reversed(current_lines):
                tail.insert(0, line)
                if estimate_tokens("\n".join(tail)) >= overlap:
                    break
            overlap_lines.clear()
            overlap_lines.extend(tail)
        current_lines = []
        current_tokens = 0
        current_start = None
        current_end = None

    for block in parse_blocks(text):
        if block.kind == "heading":
            flush()
            heading_stack = [(level, title) for level, title in heading_stack if level < block.level]
            heading_stack.append((block.level, block.text))
            current_heading = [title for _, title in heading_stack]
            pending_heading_line = f"{'#' * block.level} {block.text}"
            continue
        for piece in _split_oversized(block, maximum):
            piece_text = piece.text
            if pending_heading_line:
                piece_text = f"{pending_heading_line}\n{piece_text}"
                pending_heading_line = None
            tokens = estimate_tokens(piece_text)
            if current_lines and current_tokens + tokens > target:
                flush()
                if overlap_lines and piece.kind != "code":
                    overlap_tokens = estimate_tokens("\n".join(overlap_lines))
                    if overlap_tokens + tokens <= maximum:
                        current_lines = [*overlap_lines]
                        current_tokens = overlap_tokens
                        current_start = piece.line_start
                        current_heading = [title for _, title in heading_stack]
            if current_start is None:
                current_start = piece.line_start
                current_heading = [title for _, title in heading_stack]
            current_lines.append(piece_text)
            current_tokens += tokens
            current_end = piece.line_end
    flush()
    return chunks


def source_manifest(chunks: list[Chunk]) -> str:
    """Stable hash over ordinal + text of every chunk in the build set."""
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(f"{chunk.ordinal}:".encode())
        digest.update(chunk.text.encode())
        digest.update(b"\x00")
    return digest.hexdigest()


__all__ = ["CHUNKER_VERSION", "ESTIMATOR_VERSION", "Chunk", "chunk_document", "parse_blocks"]
