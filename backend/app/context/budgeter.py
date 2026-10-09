"""Token estimation and bounded history fitting (README 14.1).

Char-based conservative estimates until a provider tokenizer is wired in; the
estimator is recorded so results stay interpretable.
"""

ESTIMATOR_VERSION = "chars2.v1"


def estimate_tokens(text: str) -> int:
    return len(text) // 2 + 1


def fit_history(history: list[dict], *, max_tokens: int) -> tuple[list[dict], bool]:
    """Keep the most recent messages within the budget; oldest are dropped first."""
    kept: list[dict] = []
    total = 0
    for message in reversed(history):
        cost = estimate_tokens(str(message.get("content") or "")) + 10
        if total + cost > max_tokens:
            return kept[::-1], True
        kept.append(message)
        total += cost
    return kept[::-1], False
