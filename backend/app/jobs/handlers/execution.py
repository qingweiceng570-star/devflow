"""Shared execution plumbing for analyze runs (README 3.2, 5, 19).

One object owns the run's session, evidence sink, budget ledger, tool-call
tracing, event emission and terminal transitions; the per-kind flows
(pr_review, knowledge_qa) provide prompts, tools and post-processing only.
"""

import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.agents.budget import BudgetLedger
from app.agents.runtime import AgentLoop, LoopHooks, LoopOutcome, input_fingerprint
from app.context.assembler import build_initial_messages
from app.core.config import Settings
from app.core.errors import DomainError
from app.domain.contracts import AnalysisResult
from app.domain.enums import EvidenceUsage, RunStatus, StopReason
from app.domain.scope import ExecutionContext
from app.jobs.queue import LeaseLost
from app.jobs.queue import renew as renew_job
from app.persistence.models import (
    AgentRun,
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
from app.tools.executor import ToolDeps, ToolExecutor, ToolSpec

STOP_ERRORS = {"BUDGET_EXCEEDED", "STEP_LIMIT", "TOOL_LIMIT"}
STOP_MESSAGES = {
    "BUDGET_EXCEEDED": StopReason.BUDGET_EXHAUSTED,
    "STEP_LIMIT": StopReason.STEP_LIMIT,
    "TOOL_LIMIT": StopReason.TOOL_LIMIT,
}


class RunExecution:
    def __init__(
        self,
        db_factory,
        settings: Settings,
        *,
        run: Run,
        repo: Repository,
        conversation: Conversation,
        job_id: UUID,
        job_epoch: int,
        worker_id: str,
    ):
        self.db_factory = db_factory
        self.settings = settings
        self.db: DBSession = db_factory()
        self.run = self.db.get(Run, run.id)
        self.repo = repo
        self.conversation = conversation
        self.job_id = job_id
        self.job_epoch = job_epoch
        self.worker_id = worker_id
        self.collected_ids: set[UUID] = set()
        self.agent_run: AgentRun | None = None
        self._ledger: BudgetLedger | None = None

    def close(self) -> None:
        self.db.close()

    # -- lease and cancellation -------------------------------------------
    def renew_job_lease(self) -> bool:
        return renew_job(
            self.db_factory,
            job_id=self.job_id,
            worker_id=self.worker_id,
            epoch=self.job_epoch,
            lease_seconds=self.settings.job_lease_seconds,
        )

    def check_stop(self) -> None:
        if run_service.cancel_requested(self.db_factory, self.run.id):
            raise DomainError("CANCELLED", "任务已取消", 409)
        if not self.renew_job_lease():
            raise LeaseLost()

    # -- evidence ----------------------------------------------------------
    def evidence(
        self,
        *,
        source_type: str,
        content,
        locator: dict,
        source_url: str | None = None,
        observed_at=None,
        excerpt_limit: int = 500,
    ) -> UUID:
        row = capture(
            self.db,
            self.settings,
            repo_id=self.repo.id,
            source_type=source_type,
            content=content,
            locator=locator,
            source_url=source_url,
            observed_at=observed_at,
            excerpt_limit=excerpt_limit,
        )
        if row.id not in self.collected_ids:
            self.collected_ids.add(row.id)
            self.db.add(
                RunEvidence(
                    run_id=self.run.id, evidence_id=row.id, usage=EvidenceUsage.COLLECTED
                )
            )
            run_service.emit(
                self.db,
                self.run,
                "evidence.added",
                {"evidence_id": str(row.id), "source_type": source_type},
            )
            self.db.commit()
        return row.id

    # -- context -----------------------------------------------------------
    def load_history(self) -> tuple[Message | None, list[Message]]:
        user_message = self.db.scalar(
            select(Message).where(
                Message.run_id == self.run.id,
                Message.conversation_id == self.conversation.id,
            )
        )
        history = self.db.scalars(
            select(Message)
            .where(
                Message.conversation_id == self.conversation.id,
                Message.seq < (self.run.context_through_seq or 0),
            )
            .order_by(Message.seq.desc())
            .limit(self.settings.history_max_turns * 2)
        ).all()
        return user_message, list(reversed(history))

    def assemble(self, *, system_prompt: str, extra_user_messages: list[dict]):
        _, history_rows = self.load_history()
        return build_initial_messages(
            system_prompt=system_prompt,
            history_rows=history_rows,
            input_token_limit=self.settings.model_input_token_limit
            - self.settings.model_output_reserve,
            extra_user_messages=extra_user_messages,
        )

    # -- tracing -----------------------------------------------------------
    def start_agent(self, agent: str, messages: list[dict], context_info: dict) -> AgentRun:
        self.agent_run = AgentRun(
            run_id=self.run.id,
            agent=agent,
            model=self.settings.llm_model or "unconfigured",
            input_hash=input_fingerprint(messages),
            status="running",
            usage={"context": context_info},
        )
        self.db.add(self.agent_run)
        self.db.commit()
        return self.agent_run

    def fail_agent(self, error: DomainError) -> None:
        if self.agent_run is not None:
            self.agent_run.status = "failed"
            self.agent_run.error = {"code": error.code, "message": error.message}
            self.agent_run.finished_at = utcnow()
            self.db.commit()

    def finish_agent(self, outcome: LoopOutcome, context_info: dict) -> None:
        if self.agent_run is None:
            return
        self.agent_run.status = "succeeded"
        self.agent_run.usage = {
            "input_hash": self.agent_run.input_hash,
            "total_tokens": outcome.total_tokens,
            "estimated": outcome.usage_estimated,
            "model_steps": outcome.model_steps,
            "tool_calls": outcome.tool_calls,
            "context": context_info,
        }
        self.agent_run.finished_at = utcnow()
        self.db.commit()

    def make_executor(
        self,
        specs: list[ToolSpec],
        *,
        ctx: ExecutionContext,
        read,
        workspace,
        scratch: dict,
    ) -> ToolExecutor:
        deps = ToolDeps(
            settings=self.settings,
            ctx=ctx,
            repo=self.repo,
            db_factory=self.db_factory,
            github_read=read,
            workspace=workspace,
            evidence=self.evidence,
            scratch=scratch,
        )
        return ToolExecutor(specs, deps)

    def on_tool_started(self, call, logical_call_id: UUID) -> None:
        try:
            args = json.loads(call.arguments)
        except (ValueError, TypeError):
            args = {}
        self.db.add(
            ToolCall(
                run_id=self.run.id,
                agent_run_id=self.agent_run.id if self.agent_run else None,
                logical_call_id=logical_call_id,
                attempt=1,
                tool=call.name,
                args=args if isinstance(args, dict) else {},
                status="running",
            )
        )
        run_service.emit(self.db, self.run, "tool.started", {"tool": call.name})
        self.db.commit()

    def on_tool_finished(self, call, logical_call_id: UUID, result, duration_ms: int) -> None:
        row = self.db.scalar(
            select(ToolCall)
            .where(
                ToolCall.run_id == self.run.id,
                ToolCall.logical_call_id == logical_call_id,
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
            self.db,
            self.run,
            "tool.finished",
            {
                "tool": call.name,
                "status": result.status,
                "error_code": result.error.code if result.error else None,
                "evidence_ids": [str(e) for e in result.evidence_ids],
            },
        )
        self.db.commit()

    def make_hooks(self) -> LoopHooks:
        self._ledger = BudgetLedger(self.db, self.run.id, self.settings.agent_total_token_budget)
        return LoopHooks(
            check_stop=self.check_stop,
            reserve_budget=self._ledger.reserve,
            settle_budget=lambda tokens: (self._ledger.settle(tokens), self.db.commit()),
            on_tool_started=self.on_tool_started,
            on_tool_finished=self.on_tool_finished,
        )

    async def run_loop(
        self,
        model,
        executor: ToolExecutor,
        messages: list[dict],
        output_model,
        *,
        context_info: dict,
        agent: str,
    ) -> LoopOutcome | dict:
        """Run the bounded loop. Returns the outcome, or a terminal result dict
        when the run already finished (cancelled / budget-stop)."""
        self.start_agent(agent, messages, context_info)
        try:
            outcome = await AgentLoop(
                model, executor, self.settings, output_model=output_model, hooks=self.make_hooks()
            ).run(messages)
        except DomainError as error:
            self.fail_agent(error)
            if error.code == "CANCELLED":
                self.cancel()
                return {"status": "cancelled"}
            if error.code in STOP_ERRORS:
                reason = STOP_MESSAGES[error.code].value
                self.complete_partial(reason)
                return {"status": "completed", "stop": reason}
            self.fail_run(error.code, error.message)
            raise
        return outcome

    # -- terminal transitions ---------------------------------------------
    def fail_run(self, code: str, message: str) -> None:
        run_service.finalize_run(
            self.db,
            self.run,
            status=RunStatus.FAILED,
            error={"code": code, "message": message},
            partial_result={"collected_evidence": len(self.collected_ids)},
        )

    def cancel(self) -> None:
        run_service.finalize_run(self.db, self.run, status=RunStatus.CANCELLED)

    def complete_partial(self, reason: str) -> None:
        result = {
            "schema_version": "1.0",
            "kind": self.run.kind,
            "summary": "分析在预算或步数上限内未能形成完整结论；已收集的证据与工具轨迹仍可查看。",
            "verdict": "insufficient_evidence"
            if self.run.kind == "pr_review"
            else "not_applicable",
            "coverage": "partial" if self.run.kind == "pr_review" else "unavailable",
            "freshness": "unknown",
            "findings": [],
            "hypotheses": [],
            "missing_evidence": [
                {
                    "item": "分析完整性",
                    "reason": reason,
                    "impact": "本次运行未能完成完整分析",
                    "next_action": "重新发起分析或缩小范围",
                }
            ],
            "next_actions": [],
            "limitations": [reason],
            "scope": {
                "actor_id": str(self.conversation.owner_id),
                "repo_id": str(self.repo.id),
            },
        }
        run_service.finalize_run(
            self.db,
            self.run,
            status=RunStatus.COMPLETED,
            result=result,
            stop_reason=reason,
            assistant_text=run_service.render_assistant_text(result),
        )

    def finalize_success(
        self,
        result: AnalysisResult,
        *,
        stop_reason: str | None,
        outcome: LoopOutcome,
        context_info: dict,
    ) -> dict:
        result_dict = result.model_dump(mode="json")
        # One "cited" binding per distinct evidence id (uq_run_evidence_binding).
        cited_ids: set[UUID] = set()
        for finding in result.findings:
            cited_ids.update(finding.evidence_ids)
        for evidence_id in cited_ids:
            if evidence_id in self.collected_ids:
                self.db.add(
                    RunEvidence(
                        run_id=self.run.id, evidence_id=evidence_id, usage=EvidenceUsage.CITED
                    )
                )
        self.finish_agent(outcome, context_info)
        completed = run_service.finalize_run(
            self.db,
            self.run,
            status=RunStatus.COMPLETED,
            result=result_dict,
            stop_reason=stop_reason,
            assistant_text=run_service.render_assistant_text(result_dict),
        )
        return {
            "status": "completed" if completed else "state_changed",
            "verdict": result.verdict,
            "coverage": result.coverage,
            "tool_calls": outcome.tool_calls,
        }
