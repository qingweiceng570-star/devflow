"""Versioned keyword tokenizer for BM25 (README 13.2).

Rules (frozen in TOKENIZER_VERSION - index and query must share them):
- lowercase everything;
- Latin/digit runs stay as whole identifiers, and additionally split on
  snake_case and camelCase boundaries;
- CJK runs emit consecutive bigrams (single characters when the run is short);
- punctuation and whitespace separate tokens.
"""

import re

TOKENIZER_VERSION = "zh-en.v1"

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[一-鿿]+")
_CAMEL = re.compile(r"[A-Z]?[a-z]+|[0-9]+|[A-Z]+(?![a-z])")


def _split_latin(identifier: str) -> list[str]:
    pieces: list[str] = []
    for part in identifier.split("_"):
        if not part:
            continue
        lowered = part.lower()
        pieces.append(lowered)
        camel = [piece.lower() for piece in _CAMEL.findall(part)]
        if camel != [lowered]:
            pieces.extend(camel)
    return pieces


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _TOKEN_PATTERN.finditer(text):
        value = match.group(0)
        if value.isascii():
            lowered = value.lower()
            tokens.append(lowered)
            if "_" in value or any(char.isupper() for char in value):
                tokens.extend(
                    piece for piece in _split_latin(value) if piece != lowered
                )
        elif len(value) == 1:
            tokens.append(value)
        else:
            tokens.extend(value[i : i + 2] for i in range(len(value) - 1))
    return tokens


def unique_terms(text: str) -> set[str]:
    return set(tokenize(text))
