"""Analyze job handler: executes one business run (README 3.2, 11.3, 11.5).

Two flows share the same execution plumbing (RunExecution): pr_review
(refresh PR -> bounded diff scope -> verify policy -> verdict) and
knowledge_qa (route evidence between knowledge base, workspace and synced
state -> cited answer). Citation filtering and terminal transitions are
common.
"""

import asyncio
import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.agents.knowledge_qa import (
    KnowledgeDraft,
    build_question_message,
)
from app.agents.knowledge_qa import build_system_prompt as build_knowledge_prompt
from app.agents.pr_review import (
    PRReviewDraft,
    build_review_request,
    filter_evidence_refs,
    prepare_diff_scope,
)
from app.agents.pr_review import build_system_prompt as build_review_prompt
from app.bootstrap import create_model
from app.core.config import Settings
from app.core.errors import DomainError
from app.domain.contracts import AnalysisResult, RepoScope
from app.domain.enums import RunKind, RunStatus, StopReason
from app.domain.policies import VerdictFacts, enforce_pr_verdict, required_checks_status
from app.domain.scope import ExecutionContext
from app.ingestion.github_sync import apply_pull_row, create_read_client
from app.integrations.github import normalizers
from app.integrations.github.credentials import resolve_read_token
from app.integrations.github.read_adapter import GitHubRead
from app.jobs.handlers.execution import RunExecution
from app.jobs.queue import LeaseLost
from app.persistence.models import CiRun, Conversation, Repository, Run
from app.services import knowledge_service, run_service
from app.storage.local import put_object
from app.tools.executor import TOOLSET_VERSION, ToolSpec
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
from app.tools.knowledge_tools import SearchKnowledgeInput, search_knowledge
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

KNOWLEDGE_QA_TOOLS = [
    ToolSpec("search_knowledge", "在项目知识库中做混合检索（含引用、分数与降级标记）。", SearchKnowledgeInput, search_knowledge),
    ToolSpec("read_file", "读取目标 head 版本的仓库文件（指定行范围）并生成代码证据。", ReadFileInput, read_file_tool),
    ToolSpec("search_code", "在目标 head 版本中做固定字符串搜索并生成证据。", SearchCodeInput, search_code_tool),
    ToolSpec("get_repo_summary", "读取仓库健康概要（已同步范围）。", RepoSummaryInput, get_repo_summary),
    ToolSpec("get_issue", "读取单个 Issue 的描述与状态并生成证据。", GetIssueInput, get_issue),
    ToolSpec("get_pr", "读取单个 PR 的元数据并生成证据。", GetPrInput, get_pr),
    ToolSpec("get_ci_runs", "读取与目标 head 匹配的 CI 运行摘要。", GetCiRunsInput, get_ci_runs),
    ToolSpec("list_issues", "分页列出 Issue 摘要。", ListIssuesInput, list_issues),
    ToolSpec("list_pull_requests", "分页列出 PR 摘要。", ListPullsInput, list_pull_requests),
]


def handle_analyze(
    db_factory,
    settings: Settings,
    job,
    worker_id: str,
    *,
    client_factory=None,
    model_factory=None,
    workspace_factory=None,
    vector_store_factory=None,
    embedding_factory=None,
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
        if run.kind == RunKind.PR_REVIEW.value:
            flow = _execute_pr_review
        elif run.kind == RunKind.KNOWLEDGE_QA.value:
            flow = _execute_knowledge_qa
        else:
            run_service.finalize_run(
                db, run, status=RunStatus.FAILED,
                error={"code": "UNKNOWN_RUN_KIND", "message": f"未知分析类型 {run.kind}"},
            )
            return {"status": "failed"}
        try:
            return asyncio.run(
                flow(
                    db_factory,
                    settings,
                    run_id=run.id,
                    job_id=job.id,
                    job_epoch=job.lease_epoch,
                    worker_id=worker_id,
                    client_factory=client_factory,
                    model_factory=model_factory,
                    workspace_factory=workspace_factory,
                    vector_store_factory=vector_store_factory,
                    embedding_factory=embedding_factory,
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


def _start_execution(
    db_factory, settings, *, run_id, job_id, job_epoch, worker_id, client_factory
):
    """Load the run context and open the shared execution object."""
    bootstrap_db = db_factory()
    try:
        run = bootstrap_db.get(Run, run_id)
        repo = bootstrap_db.get(Repository, run.repo_id)
        conversation = bootstrap_db.get(Conversation, run.conversation_id)
        token = resolve_read_token(settings, repo.credential_ref)
        maker = client_factory or create_read_client
        read = GitHubRead(maker(settings, token), repo.full_name)
        execution = RunExecution(
            db_factory,
            settings,
            run=run,
            repo=repo,
            conversation=conversation,
            job_id=job_id,
            job_epoch=job_epoch,
            worker_id=worker_id,
        )
        return execution, read, token
    finally:
        bootstrap_db.close()


def _model_maker(model_factory):
    return model_factory or (lambda s: create_model(s, "configured"))


def _make_workspace(workspace_factory):
    return workspace_factory or (lambda s, t: WorkspaceManager(s, token=t))


async def _run_flow(
    execution: RunExecution,
    *,
    messages: list[dict],
    context_info: dict,
    output_model,
    specs: list[ToolSpec],
    read,
    workspace,
    scratch: dict,
    ctx: ExecutionContext,
    agent: str,
    model_maker,
):
    """Shared loop invocation with model construction and lease-safety."""
    executor = execution.make_executor(specs, ctx=ctx, read=read, workspace=workspace, scratch=scratch)
    try:
        model = model_maker(execution.settings)
    except DomainError as error:
        execution.fail_agent(error)
        execution.fail_run(error.code, error.message)
        raise
    return await execution.run_loop(
        model, executor, messages, output_model, context_info=context_info, agent=agent
    )


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
    vector_store_factory=None,
    embedding_factory=None,
) -> dict:
    model_maker = _model_maker(model_factory)
    make_workspace = _make_workspace(workspace_factory)
    execution, read, token = _start_execution(
        db_factory,
        settings,
        run_id=run_id,
        job_id=job_id,
        job_epoch=job_epoch,
        worker_id=worker_id,
        client_factory=client_factory,
    )
    db, run, repo, conversation = execution.db, execution.run, execution.repo, execution.conversation
    try:
        pr_number = run.target.get("pr_number")
        # --- refresh target PR live and fix base/head SHA (README 3.2 step 3) ---
        try:
            raw_pr = read.get_pr_live(pr_number)
        except DomainError as error:
            execution.fail_run(error.code, "无法读取目标 PR：不存在或无权访问")
            raise
        pr_data = normalizers.normalize_pull(raw_pr)
        body_ref = None
        if pr_data["body"]:
            body_ref, _ = put_object(
                settings.storage_root, repo.id, "bodies", pr_data["body"].encode()
            )
        pr_data["body_ref"] = body_ref
        apply_pull_row(db, repo.id, pr_data)
        db.commit()
        head_sha = pr_data["head_sha"]
        base_sha = pr_data["base_sha"]
        mergeable = raw_pr.get("mergeable")
        is_draft = bool(pr_data["draft"])
        if not head_sha or not base_sha:
            execution.fail_run("SOURCE_UNAVAILABLE", "PR 缺少可固定的版本信息")
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
                settings, read, repo.full_name, pr_number, pr_data["html_url"], head_sha,
                execution.evidence,
            )
        except DomainError as error:
            execution.fail_run(error.code, "无法获取 PR 差异")
            raise
        scratch = {"pr_diff": scope}

        required_checks = list((repo.config_json or {}).get("required_checks") or [])
        ci_runs = _ci_run_dicts(db, repo.id, head_sha)
        checks_configured, checks_passed, checks_detail = required_checks_status(
            required_checks, ci_runs
        )

        # --- assemble context ---
        user_message, _history = execution.load_history()
        system_prompt = build_review_prompt(repo.full_name, required_checks)
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
        messages, context_info = execution.assemble(
            system_prompt=system_prompt, extra_user_messages=review_request
        )

        outcome = await _run_flow(
            execution,
            messages=messages,
            context_info=context_info,
            output_model=PRReviewDraft,
            specs=PR_REVIEW_TOOLS,
            read=read,
            workspace=make_workspace(settings, token),
            scratch=scratch,
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
            agent="pr_review",
            model_maker=model_maker,
        )
        if isinstance(outcome, dict):
            return outcome

        # --- post-processing: citations, freshness re-check, verdict policy ---
        draft, citation_notes = filter_evidence_refs(outcome.parsed, execution.collected_ids)
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
        if coverage == "partial":
            if scope["coverage"]["capped_files"]:
                limitations.append("文件数达到分析上限")
            elif scope["coverage"]["capped_bytes"]:
                limitations.append("变更正文达到分析上限")

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
            limitations.append("结论按规则降级：" + "；".join(hard_reasons + gap_reasons))
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
        return execution.finalize_success(
            result, stop_reason=stop_reason, outcome=outcome, context_info=context_info
        )
    finally:
        execution.close()


async def _execute_knowledge_qa(
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
    vector_store_factory=None,
    embedding_factory=None,
) -> dict:
    model_maker = _model_maker(model_factory)
    make_workspace = _make_workspace(workspace_factory)
    execution, read, token = _start_execution(
        db_factory,
        settings,
        run_id=run_id,
        job_id=job_id,
        job_epoch=job_epoch,
        worker_id=worker_id,
        client_factory=client_factory,
    )
    db, run, repo, conversation = execution.db, execution.run, execution.repo, execution.conversation
    try:
        user_message, _history = execution.load_history()
        if user_message is None:
            execution.fail_run("RUN_CONTEXT_MISSING", "缺少用户消息")
            return {"status": "failed"}

        # Fix the default-branch head for code reads; failure degrades to
        # database/knowledge tools only (the run continues).
        head_sha = None
        if repo.default_branch:
            try:
                head_sha = read.get_branch_head(repo.default_branch)
            except DomainError:
                head_sha = None
        index_summary = knowledge_service.index_status(db, repo.id)
        active = index_summary.get("active_generation")
        system_prompt = build_knowledge_prompt(
            repo.full_name,
            {
                "generation_id": active["id"] if active else None,
                "chunk_count": active["chunk_count"] if active else 0,
            },
        )
        question_messages = build_question_message(user_message.content, index_summary)
        messages, context_info = execution.assemble(
            system_prompt=system_prompt, extra_user_messages=question_messages
        )

        scratch = {
            "vector_store_factory": vector_store_factory,
            "embedding_factory": embedding_factory,
        }
        outcome = await _run_flow(
            execution,
            messages=messages,
            context_info=context_info,
            output_model=KnowledgeDraft,
            specs=KNOWLEDGE_QA_TOOLS,
            read=read,
            workspace=make_workspace(settings, token),
            scratch=scratch,
            ctx=ExecutionContext(
                actor_id=conversation.owner_id,
                repo_id=repo.id,
                credential_ref=repo.credential_ref,
                run_id=run.id,
                conversation_id=conversation.id,
                head_sha=head_sha,
                prompt_version="knowledge_qa.v1",
                toolset_version=TOOLSET_VERSION,
                config_snapshot=run.config_snapshot or {},
                lease_epoch=job_epoch,
            ),
            agent="knowledge_qa",
            model_maker=model_maker,
        )
        if isinstance(outcome, dict):
            return outcome

        draft, citation_notes = filter_evidence_refs(outcome.parsed, execution.collected_ids)
        limitations = list(draft.limitations) + citation_notes
        if context_info["history_trimmed"]:
            limitations.append("更早的对话历史因预算被省略")
        if not active:
            limitations.append("知识库尚未建立索引，历史资料检索不可用")

        has_evidence = bool(execution.collected_ids)
        result = AnalysisResult(
            kind="knowledge_qa",
            summary=draft.summary,
            verdict="not_applicable",
            coverage="complete" if has_evidence else "unavailable",
            freshness="fresh" if (active and not index_summary.get("pending_changes")) else "unknown",
            findings=draft.findings,
            hypotheses=draft.hypotheses,
            missing_evidence=draft.missing_evidence,
            next_actions=draft.next_actions,
            limitations=limitations,
            scope=RepoScope(actor_id=conversation.owner_id, repo_id=repo.id),
        )
        return execution.finalize_success(
            result, stop_reason=None, outcome=outcome, context_info=context_info
        )
    finally:
        execution.close()


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
