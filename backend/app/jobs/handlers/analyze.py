"""Analyze job handler: executes one PR review run (README 3.2, 11.3).

Pipeline: refresh the target PR live and fix base/head -> prepare the bounded
diff scope (each file captured as evidence) -> run the bounded agent loop with
the verified tool set -> filter citations -> enforce verdict policy with a
final head re-check -> persist the result, assistant message and terminal
event atomically.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.agents.budget import BudgetLedger
from app.agents.pr_review import (
    PRReviewDraft,
    build_review_request,
    build_system_prompt,
    filter_evidence_refs,
    prepare_diff_scope,
)
from app.agents.runtime import AgentLoop, LoopHooks, input_fingerprint
from app.bootstrap import create_model
from app.context.assembler import build_initial_messages
from app.core.config import Settings
from app.core.errors import DomainError
from app.domain.contracts import AnalysisResult, RepoScope
from app.domain.enums import EvidenceUsage, RunStatus, StopReason
from app.domain.policies import VerdictFacts, enforce_pr_verdict, required_checks_status
from app.domain.scope import ExecutionContext
from app.ingestion.github_sync import apply_pull_row, create_read_client
from app.integrations.github import normalizers
from app.integrations.github.credentials import resolve_read_token
from app.integrations.github.read_adapter import GitHubRead
from app.jobs.queue import LeaseLost
from app.jobs.queue import renew as renew_job
from app.persistence.models import (
    AgentRun,
    CiRun,
    Conversation,
    Message,
    Repository,
    Run,
    RunEvidence,
    ToolCall,
    utcnow,
)
from app.services import run_service
from app.services.evidence_service import capture
from app.storage.local import put_object
from app.tools.executor import TOOLSET_VERSION, ToolDeps, ToolExecutor, ToolSpec
from app.tools.github_tools import (
    GetCiRunsInput,
    GetIssueInput,
    GetPrDiffInput,
    GetPrInput,
    ListIssuesInput,
    ListPullsInput,
    ReadFileInput,
    RepoSummaryInput,
    SearchCodeInput,
    get_ci_runs,
    get_issue,
    get_pr,
    get_pr_diff,
    get_repo_summary,
    list_issues,
    list_pull_requests,
)
from app.tools.workspace_tools import read_file_tool, search_code_tool
from app.workspace.manager import WorkspaceManager

log = logging.getLogger("devflow.analyze")

PR_REVIEW_TOOLS = [
    ToolSpec("get_repo_summary", "读取仓库健康概要（已同步范围），用于整体背景。", RepoSummaryInput, get_repo_summary),
    ToolSpec("list_issues", "分页列出 Issue 摘要。", ListIssuesInput, list_issues),
    ToolSpec("get_issue", "读取单个 Issue 的描述与状态并生成证据。", GetIssueInput, get_issue),
    ToolSpec("list_pull_requests", "分页列出 PR 摘要。", ListPullsInput, list_pull_requests),
    ToolSpec("get_pr", "读取单个 PR 的元数据并生成证据。", GetPrInput, get_pr),
    ToolSpec("get_pr_diff", "分页读取目标 PR 已准备的差异补丁（含每文件证据 ID 与覆盖度）。", GetPrDiffInput, get_pr_diff),
    ToolSpec("get_ci_runs", "读取与目标 head 匹配的 CI 运行摘要与失败任务，并生成证据。", GetCiRunsInput, get_ci_runs),
    ToolSpec("read_file", "读取目标 head 版本的仓库文件（指定行范围）并生成代码证据。", ReadFileInput, read_file_tool),
    ToolSpec("search_code", "在目标 head 版本中做固定字符串搜索并生成证据。", SearchCodeInput, search_code_tool),
]

STOP_ERRORS = {"BUDGET_EXCEEDED", "STEP_LIMIT", "TOOL_LIMIT"}


def handle_analyze(
    db_factory,
    settings: Settings,
    job,
    worker_id: str,
    *,
    client_factory=None,
    model_factory=None,
    workspace_factory=None,
) -> dict:
    with db_factory() as db:
        run = db.get(Run, job.entity_id)
        if run is None:
            return {"status": "missing"}
        if run.status in run_service.TERMINAL_RUN_STATUSES:
            return {"status": run.status}
        repo = db.get(Repository, run.repo_id)
        conversation = db.get(Conversation, run.conversation_id) if run.conversation_id else None
        if repo is None or conversation is None:
            run_service.finalize_run(
                db, run, status=RunStatus.FAILED,
                error={"code": "RUN_CONTEXT_MISSING", "message": "运行上下文缺失"},
            )
            return {"status": "failed"}
        if repo.state != "active":
            run_service.finalize_run(
                db, run, status=RunStatus.FAILED,
                error={"code": "REPO_DISABLED", "message": "仓库已禁用"},
            )
            return {"status": "failed"}
        if run.cancel_requested_at is not None:
            run_service.finalize_run(db, run, status=RunStatus.CANCELLED, stop_reason=None)
            return {"status": "cancelled"}
        try:
            run_service.mark_running(db, run)
        except DomainError:
            return {"status": "state_changed"}
        try:
            return asyncio.run(
                _execute_pr_review(
                    db_factory,
                    settings,
                    run_id=run.id,
                    job_id=job.id,
                    job_epoch=job.lease_epoch,
                    worker_id=worker_id,
                    client_factory=client_factory,
                    model_factory=model_factory,
                    workspace_factory=workspace_factory,
                )
            )
        except LeaseLost:
            return {"status": "lease_lost"}
        except Exception:
            # Safety net: an unexpected handler bug must still leave the run in
            # a terminal state instead of a forever-"running" record.
            with db_factory() as guard_db:
                guard_run = guard_db.get(Run, run.id)
                if guard_run is not None and guard_run.status not in run_service.TERMINAL_RUN_STATUSES:
                    run_service.finalize_run(
                        guard_db,
                        guard_run,
                        status=RunStatus.FAILED,
                        error={"code": "INTERNAL_ERROR", "message": "分析执行出现未预期错误"},
                    )
            raise


async def _execute_pr_review(
    db_factory,
    settings: Settings,
    *,
    run_id: UUID,
    job_id: UUID,
    job_epoch: int,
    worker_id: str,
    client_factory,
    model_factory,
    workspace_factory,
) -> dict:
    maker = client_factory or create_read_client
    model_maker = model_factory or (lambda s: create_model(s, "configured"))
    make_workspace = workspace_factory or (lambda s, t: WorkspaceManager(s, token=t))
    pr_number = None

    def renew_job_lease() -> bool:
        return renew_job(
            db_factory,
            job_id=job_id,
            worker_id=worker_id,
            epoch=job_epoch,
            lease_seconds=settings.job_lease_seconds,
        )

    with db_factory() as db:
        run = db.get(Run, run_id)
        repo = db.get(Repository, run.repo_id)
        conversation = db.get(Conversation, run.conversation_id)
        pr_number = run.target.get("pr_number")
        token = resolve_read_token(settings, repo.credential_ref)
        read = GitHubRead(maker(settings, token), repo.full_name)

        collected_ids: set[UUID] = set()

        def evidence_sink(
            *,
            source_type: str,
            content,
            locator: dict,
            source_url: str | None = None,
            observed_at=None,
            excerpt_limit: int = 500,
        ) -> UUID:
            row = capture(
                db,
                settings,
                repo_id=repo.id,
                source_type=source_type,
                content=content,
                locator=locator,
                source_url=source_url,
                observed_at=observed_at,
                excerpt_limit=excerpt_limit,
            )
            if row.id not in collected_ids:
                collected_ids.add(row.id)
                db.add(
                    RunEvidence(
                        run_id=run.id, evidence_id=row.id, usage=EvidenceUsage.COLLECTED
                    )
                )
                run_service.emit(
                    db,
                    run,
                    "evidence.added",
                    {"evidence_id": str(row.id), "source_type": source_type},
                )
                db.commit()
            return row.id

        def fail(code: str, message: str) -> None:
            run_service.finalize_run(
                db,
                run,
                status=RunStatus.FAILED,
                error={"code": code, "message": message},
                partial_result={"collected_evidence": len(collected_ids)},
            )

        def complete_partial(reason: str, summary: str) -> dict:
            result = {
                "schema_version": "1.0",
                "kind": "pr_review",
                "summary": summary,
                "verdict": "insufficient_evidence",
                "coverage": "partial",
                "freshness": "unknown",
                "findings": [],
                "hypotheses": [],
                "missing_evidence": [
                    {
                        "item": "分析完整性",
                        "reason": reason,
                        "impact": "本次运行未能完成完整审查",
                        "next_action": "重新发起分析或缩小范围",
                    }
                ],
                "next_actions": [],
                "limitations": [reason],
                "scope": {"actor_id": str(conversation.owner_id), "repo_id": str(repo.id)},
            }
            completed = run_service.finalize_run(
                db,
                run,
                status=RunStatus.COMPLETED,
                result=result,
                stop_reason=reason,
                assistant_text=run_service.render_assistant_text(result),
            )
            return {"status": "completed" if completed else "state_changed", "stop": reason}

        # --- refresh target PR live and fix base/head SHA (README 3.2 step 3) ---
        try:
            raw_pr = read.get_pr_live(pr_number)
        except DomainError as error:
            fail(error.code, "无法读取目标 PR：不存在或无权访问")
            raise
        pr_data = normalizers.normalize_pull(raw_pr)
        body_ref = None
        if pr_data["body"]:
            body_ref, _ = put_object(
                settings.storage_root, repo.id, "bodies", pr_data["body"].encode()
            )
        pr_data["body_ref"] = body_ref
        pr_row, _ = apply_pull_row(db, repo.id, pr_data)
        db.commit()
        head_sha = pr_data["head_sha"]
        base_sha = pr_data["base_sha"]
        mergeable = raw_pr.get("mergeable")
        is_draft = bool(pr_data["draft"])
        if not head_sha or not base_sha:
            fail("SOURCE_UNAVAILABLE", "PR 缺少可固定的版本信息")
            return {"status": "failed"}
        run.target = {
            "pr_number": pr_number,
            "head_sha": head_sha,
            "base_sha": base_sha,
            "refreshed_at": datetime.now(UTC).isoformat(),
        }
        db.commit()

        # --- prepare the bounded diff scope; every patch becomes evidence ---
        try:
            scope = prepare_diff_scope(
                settings,
                read,
                repo.full_name,
                pr_number,
                pr_data["html_url"],
                head_sha,
                evidence_sink,
            )
        except DomainError as error:
            fail(error.code, "无法获取 PR 差异")
            raise
        scratch = {"pr_diff": scope}

        required_checks = list((repo.config_json or {}).get("required_checks") or [])
        ci_runs = _ci_run_dicts(db, repo.id, head_sha)
        checks_configured, checks_passed, checks_detail = required_checks_status(
            required_checks, ci_runs
        )

        # --- assemble context ---
        user_message = db.scalar(
            select(Message).where(
                Message.run_id == run.id, Message.conversation_id == conversation.id
            )
        )
        history_rows = db.scalars(
            select(Message)
            .where(
                Message.conversation_id == conversation.id,
                Message.seq < (run.context_through_seq or 0),
            )
            .order_by(Message.seq.desc())
            .limit(settings.history_max_turns * 2)
        ).all()
        history_rows = list(reversed(history_rows))
        system_prompt = build_system_prompt(repo.full_name, required_checks)
        review_request = build_review_request(
            {
                "number": pr_number,
                "title": pr_data["title"],
                "author": pr_data["author_login"],
                "base_ref": pr_data["base_ref"],
                "head_ref": pr_data["head_ref"],
                "head_sha": head_sha,
                "draft": is_draft,
                "html_url": pr_data["html_url"],
                "body_excerpt": (pr_data["body"] or "")[:1500],
            },
            scope,
            {
                "configured": required_checks,
                "all_passed": checks_passed,
                "detail": checks_detail,
                "runs_for_head": ci_runs,
            },
            user_message.content if user_message else None,
        )
        messages, context_info = build_initial_messages(
            system_prompt=system_prompt,
            history_rows=history_rows,
            input_token_limit=settings.model_input_token_limit - settings.model_output_reserve,
            extra_user_messages=review_request,
        )

        # --- trace records ---
        agent_run = AgentRun(
            run_id=run.id,
            agent="pr_review",
            model=settings.llm_model or "unconfigured",
            input_hash=input_fingerprint(messages),
            status="running",
            usage={"context": context_info},
        )
        db.add(agent_run)
        db.commit()

        def check_stop() -> None:
            if run_service.cancel_requested(db_factory, run.id):
                raise DomainError("CANCELLED", "任务已取消", 409)
            if not renew_job_lease():
                raise LeaseLost()

        ledger = BudgetLedger(db, run.id, settings.agent_total_token_budget)

        def on_model_step(entry: dict) -> None:
            pass

        def on_tool_started(call, logical_call_id: UUID) -> None:
            try:
                args = json.loads(call.arguments)
            except (ValueError, TypeError):
                args = {}
            db.add(
                ToolCall(
                    run_id=run.id,
                    agent_run_id=agent_run.id,
                    logical_call_id=logical_call_id,
                    attempt=1,
                    tool=call.name,
                    args=args if isinstance(args, dict) else {},
                    status="running",
                )
            )
            run_service.emit(db, run, "tool.started", {"tool": call.name})
            db.commit()

        def on_tool_finished(call, logical_call_id: UUID, result, duration_ms: int) -> None:
            row = db.scalar(
                select(ToolCall)
                .where(
                    ToolCall.run_id == run.id, ToolCall.logical_call_id == logical_call_id
                )
                .limit(1)
            )
            if row is not None:
                row.status = result.status
                row.result = result.model_dump(mode="json")
                row.error_code = result.error.code if result.error else None
                row.duration_ms = duration_ms
                row.finished_at = utcnow()
            run_service.emit(
                db,
                run,
                "tool.finished",
                {
                    "tool": call.name,
                    "status": result.status,
                    "error_code": result.error.code if result.error else None,
                    "evidence_ids": [str(e) for e in result.evidence_ids],
                },
            )
            db.commit()

        hooks = LoopHooks(
            check_stop=check_stop,
            reserve_budget=ledger.reserve,
            settle_budget=lambda tokens: (ledger.settle(tokens), db.commit()),
            on_model_step=on_model_step,
            on_tool_started=on_tool_started,
            on_tool_finished=on_tool_finished,
        )
        deps = ToolDeps(
            settings=settings,
            ctx=ExecutionContext(
                actor_id=conversation.owner_id,
                repo_id=repo.id,
                credential_ref=repo.credential_ref,
                run_id=run.id,
                conversation_id=conversation.id,
                pr_number=pr_number,
                base_sha=base_sha,
                head_sha=head_sha,
                prompt_version=run_service.PROMPT_VERSION,
                toolset_version=TOOLSET_VERSION,
                config_snapshot=run.config_snapshot or {},
                lease_epoch=job_epoch,
            ),
            repo=repo,
            db_factory=db_factory,
            github_read=read,
            workspace=make_workspace(settings, token),
            evidence=evidence_sink,
            scratch=scratch,
        )
        executor = ToolExecutor(PR_REVIEW_TOOLS, deps)
        try:
            model = model_maker(settings)
        except DomainError as error:
            agent_run.status = "failed"
            agent_run.error = {"code": error.code, "message": error.message}
            agent_run.finished_at = utcnow()
            db.commit()
            fail(error.code, error.message)
            raise

        try:
            outcome = await AgentLoop(
                model,
                executor,
                settings,
                output_model=PRReviewDraft,
                hooks=hooks,
            ).run(messages)
        except DomainError as error:
            agent_run.status = "failed"
            agent_run.error = {"code": error.code, "message": error.message}
            agent_run.finished_at = utcnow()
            db.commit()
            if error.code == "CANCELLED":
                run_service.finalize_run(db, run, status=RunStatus.CANCELLED)
                return {"status": "cancelled"}
            if error.code in STOP_ERRORS:
                reason = {
                    "BUDGET_EXCEEDED": StopReason.BUDGET_EXHAUSTED,
                    "STEP_LIMIT": StopReason.STEP_LIMIT,
                    "TOOL_LIMIT": StopReason.TOOL_LIMIT,
                }[error.code]
                return complete_partial(
                    reason.value,
                    "分析在预算或步数上限内未能形成完整结论；已收集的证据与工具轨迹仍可查看。",
                )
            fail(error.code, error.message)
            raise

        # --- post-processing: citations, freshness re-check, verdict policy ---
        draft, citation_notes = filter_evidence_refs(outcome.parsed, collected_ids)
        limitations = list(draft.limitations) + citation_notes
        if scope["unreviewed"]:
            note = "以下文件未获得补丁或超出预算，未纳入审查：" + ", ".join(
                scope["unreviewed"][:30]
            )
            if scope["coverage"].get("more_files_exist"):
                note += "（还有更多文件未枚举）"
            limitations.append(note)
        if context_info["history_trimmed"]:
            limitations.append("更早的对话历史因预算被省略")

        freshness = "fresh"
        stop_reason = None
        try:
            recheck = read.get_pr_live(pr_number)
            if recheck.get("head", {}).get("sha") != head_sha:
                freshness = "stale"
                stop_reason = StopReason.HEAD_CHANGED.value
                limitations.append("输出前复查发现 PR head 已变化，结论对应旧版本")
        except DomainError:
            freshness = "unknown"
            limitations.append("输出前无法复查 PR 版本")

        coverage = "complete" if scope["coverage"]["complete"] else "partial"
        coverage_note = None
        if coverage == "partial":
            if scope["coverage"]["capped_files"]:
                coverage_note = "文件数达到分析上限"
            elif scope["coverage"]["capped_bytes"]:
                coverage_note = "变更正文达到分析上限"
            if coverage_note:
                limitations.append(coverage_note)

        facts = VerdictFacts(
            blocking_findings=sum(
                1 for finding in draft.findings if finding.severity in {"critical", "high"}
            ),
            coverage=coverage,
            freshness=freshness,
            draft=is_draft,
            mergeable=mergeable if isinstance(mergeable, bool) else None,
            required_checks_configured=checks_configured,
            required_checks_passed=checks_passed,
            details=checks_detail,
        )
        final_verdict, hard_reasons, gap_reasons = enforce_pr_verdict(draft.verdict, facts)
        if final_verdict != draft.verdict:
            limitations.append(
                "结论按规则降级："
                + "；".join(hard_reasons + gap_reasons)
            )
        if checks_configured and checks_detail:
            limitations.extend(f"必需检查：{item}" for item in checks_detail[:10])

        result = AnalysisResult(
            kind="pr_review",
            summary=draft.summary,
            verdict=final_verdict,
            coverage=coverage,
            freshness=freshness,
            findings=draft.findings,
            hypotheses=draft.hypotheses,
            missing_evidence=draft.missing_evidence,
            next_actions=draft.next_actions,
            limitations=limitations,
            scope=RepoScope(actor_id=conversation.owner_id, repo_id=repo.id),
        )
        result_dict = result.model_dump(mode="json")

        # One "cited" binding per distinct evidence id, even when several
        # findings reuse the same evidence (uq_run_evidence_binding).
        cited_ids: set[UUID] = set()
        for finding in result.findings:
            cited_ids.update(finding.evidence_ids)
        for evidence_id in cited_ids:
            if evidence_id in collected_ids:
                db.add(
                    RunEvidence(
                        run_id=run.id,
                        evidence_id=evidence_id,
                        usage=EvidenceUsage.CITED,
                    )
                )
        agent_run.status = "succeeded"
        agent_run.usage = {
            "input_hash": agent_run.input_hash,
            "total_tokens": outcome.total_tokens,
            "estimated": outcome.usage_estimated,
            "model_steps": outcome.model_steps,
            "tool_calls": outcome.tool_calls,
            "context": context_info,
        }
        agent_run.finished_at = utcnow()
        db.commit()

        completed = run_service.finalize_run(
            db,
            run,
            status=RunStatus.COMPLETED,
            result=result_dict,
            stop_reason=stop_reason,
            assistant_text=run_service.render_assistant_text(result_dict),
        )
        return {
            "status": "completed" if completed else "state_changed",
            "verdict": final_verdict,
            "coverage": coverage,
            "tool_calls": outcome.tool_calls,
        }


def _ci_run_dicts(db: DBSession, repo_id: UUID, head_sha: str) -> list[dict]:
    runs = db.scalars(
        select(CiRun)
        .where(CiRun.repo_id == repo_id, CiRun.head_sha == head_sha)
        .order_by(CiRun.source_created_at.desc())
        .limit(20)
    ).all()
    return [
        {
            "workflow": run.workflow_name,
            "status": run.status,
            "conclusion": run.conclusion,
            "attempt": run.attempt,
            "started_at": run.run_started_at.isoformat() if run.run_started_at else None,
        }
        for run in runs
    ]
