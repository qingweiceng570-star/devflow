"""Bounded plain-text job logs; never extract or persist an upstream archive."""

import re

ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
ERROR = re.compile(
    r"assertionerror|traceback|\bfailed\b|\berror\b|exception|exit code [1-9]|timed?\s*out|out of memory|unable to|could not",
    re.I,
)
SECRET = re.compile(
    r"(?i)(\b(?:password|passwd|api[_-]?key|access[_-]?token|secret|authorization)\b\s*[:=]\s*)(?:Bearer\s+)?[^\s,;]+"
)
TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,}|sk-[A-Za-z0-9_-]{16,})\b"
)


def redact(text: str, known_secrets=()) -> str:
    for secret in known_secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = TOKEN.sub("[REDACTED]", text)
    text = SECRET.sub(r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text)
    return text


def parse_log(
    raw: bytes, *, truncated: bool = False, max_blocks: int = 8, known_secrets=()
) -> dict:
    if raw.startswith(b"PK\x03\x04"):
        return {
            "blocks": [],
            "truncated": True,
            "line_count": 0,
            "warnings": ["预期纯文本日志，拒绝压缩包"],
        }
    # Keep one output line for each source line; redact before clipping so long
    # secret values cannot be leaked as partial prefixes.
    text = ANSI.sub("", raw.decode("utf-8", errors="replace"))
    lines = []
    clipped = False
    for original in text.splitlines():
        line = redact(original, known_secrets)
        line = "".join(c for c in line if c == "\t" or ord(c) >= 32)
        clipped |= len(line) > 2000
        lines.append(line[:2000])
    windows: list[list[int]] = []
    for index, line in enumerate(lines):
        if ERROR.search(line):
            start, end = max(0, index - 30), min(len(lines), index + 31)
            if windows and start <= windows[-1][1]:
                # Avoid merging an arbitrarily large failure log into one block.
                if end - windows[-1][0] <= 90:
                    windows[-1][1] = end
                elif index >= windows[-1][1]:
                    windows.append([index, end])
            else:
                windows.append([start, end])
    if not windows and lines:
        windows = [[max(0, len(lines) - 61), len(lines)]]
    omitted = len(windows) > max_blocks
    blocks = []
    for start, end in windows[:max_blocks]:
        selected = lines[start:end]
        # A bounded excerpt may retain fewer than 61 lines when lines are long.
        total = 0
        kept = []
        for line in selected:
            if total + len(line) > 12000:
                clipped = True
                break
            kept.append(line)
            total += len(line) + 1
        if kept:
            blocks.append(
                {"line_start": start + 1, "line_end": start + len(kept), "text": "\n".join(kept)}
            )
    warnings = []
    if truncated or clipped or omitted:
        warnings.append("日志超出字节、行长或候选块上限，仅分析保留窗口")
    return {
        "blocks": blocks,
        "truncated": truncated or clipped or omitted,
        "line_count": len(lines),
        "warnings": warnings,
    }
