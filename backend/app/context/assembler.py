"""Assemble the initial message list for one agent run (README 14.1, 14.2).

Only committed conversation history up to the run's frozen sequence is used;
later messages can never leak into an active run. Cutting is done oldest-first
and every source keeps its label (system rules, history, current request).
"""

from app.context.budgeter import estimate_tokens, fit_history

TRIM_NOTICE = "（更早的对话记录因上下文预算被省略）"


def build_initial_messages(
    *,
    system_prompt: str,
    history_rows: list,
    input_token_limit: int,
    extra_user_messages: list[dict] | None = None,
) -> tuple[list[dict], dict]:
    extra = list(extra_user_messages or [])
    history = [{"role": row.role, "content": row.content} for row in history_rows]
    extra_cost = sum(
        estimate_tokens(str(message.get("content") or "")) + 10 for message in extra
    )
    system_cost = estimate_tokens(system_prompt) + 10
    history_budget = max(0, input_token_limit - system_cost - extra_cost)
    kept, trimmed = fit_history(history, max_tokens=history_budget)
    messages = [{"role": "system", "content": system_prompt}]
    if trimmed:
        messages.append({"role": "system", "content": TRIM_NOTICE})
    messages.extend(kept)
    messages.extend(extra)
    info = {
        "history_kept": len(kept),
        "history_trimmed": trimmed,
        "estimated_input_tokens": sum(
            estimate_tokens(str(message.get("content") or "")) + 10 for message in messages
        ),
    }
    return messages, info
