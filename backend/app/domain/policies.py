"""Rule-based policy checks (README 6.4, 11.3): the backend may downgrade a
model verdict but can never invent findings the model did not produce."""

from dataclasses import dataclass, field


@dataclass
class VerdictFacts:
    blocking_findings: int = 0
    coverage: str = "partial"  # complete | partial | unavailable
    freshness: str = "unknown"  # fresh | stale | unknown
    draft: bool = False
    mergeable: bool | None = None  # None = GitHub has not computed it yet
    required_checks_configured: bool = False
    required_checks_passed: bool | None = None  # None = unknown for this head
    details: list[str] = field(default_factory=list)


def enforce_pr_verdict(suggested: str, facts: VerdictFacts) -> tuple[str, list[str], list[str]]:
    """Return (final_verdict, hard_block_reasons, information_gap_reasons).

    `recommend_merge` is only kept when every condition is verifiably green;
    anything else downgrades to `hold` (a concrete blocker) or
    `insufficient_evidence` (an information gap), never the other direction.
    """
    hard: list[str] = []
    gaps: list[str] = []
    if facts.blocking_findings:
        hard.append(f"存在 {facts.blocking_findings} 条高严重度发现")
    if facts.mergeable is False:
        hard.append("存在合并冲突")
    if facts.required_checks_configured and facts.required_checks_passed is False:
        hard.append("必需检查未通过")
    if facts.draft:
        hard.append("PR 仍为草稿")
    if facts.coverage != "complete":
        gaps.append("审查范围不完整")
    if facts.freshness != "fresh":
        gaps.append("目标版本已变化或新鲜度未知")
    if facts.mergeable is None:
        gaps.append("GitHub 合并状态未知")
    if not facts.required_checks_configured:
        gaps.append("没有可用的必需检查清单")
    elif facts.required_checks_passed is None:
        gaps.append("必需检查在目标版本上没有可确认的结果")

    if suggested != "recommend_merge":
        return suggested, hard, gaps
    if hard:
        return "hold", hard, gaps
    if gaps:
        return "insufficient_evidence", hard, gaps
    return "recommend_merge", hard, gaps


def required_checks_status(
    configured: list[str], runs: list[dict]
) -> tuple[bool, bool | None, list[str]]:
    """Evaluate required checks against runs matched to the target head.

    Returns (configured, all_passed, detail). A check name may match a workflow
    name exactly; the latest attempt wins. Missing checks count as not passed.
    """
    if not configured:
        return False, None, []
    latest: dict[str, dict] = {}
    for run in runs:
        name = run.get("workflow") or ""
        previous = latest.get(name)
        if previous is None or (run.get("started_at") or "") >= (previous.get("started_at") or ""):
            latest[name] = run
    detail: list[str] = []
    all_passed = True
    for check in configured:
        run = latest.get(check)
        if run is None:
            all_passed = False
            detail.append(f"{check}: 无匹配运行")
            continue
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            all_passed = False
            detail.append(f"{check}: {run.get('status')}/{run.get('conclusion')}")
    return True, all_passed, detail
