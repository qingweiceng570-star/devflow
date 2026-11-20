"""Reproducible reports over observed events, with explicit coverage boundaries."""

import json
from collections import Counter

from sqlalchemy import select

from app.domain.contracts import AnalysisResult, RepoScope
from app.persistence.models import (
    ActivityEvent,
    Conversation,
    Repository,
    Run,
    RunEvidence,
    WeeklyReport,
)
from app.services import run_service
from app.services.evidence_service import capture
from app.workflows.contracts import WeeklyTarget


def execute_weekly(factory, settings, job, worker_id):
    with factory() as db:
        run = db.get(Run, job.entity_id)
        if run.status in run_service.TERMINAL_RUN_STATUSES:
            return {"status": run.status}
        if run.cancel_requested_at:
            run_service.finalize_run(db, run, status="cancelled")
            return {"status": "cancelled"}
        repo = db.get(Repository, run.repo_id)
        if repo.state != "active":
            run_service.finalize_run(
                db, run, status="failed", error={"code": "REPO_DISABLED", "message": "仓库已禁用"}
            )
            return {"status": "failed"}
        target = WeeklyTarget.model_validate(run.target)
        run_service.mark_running(db, run)
        events = db.scalars(
            select(ActivityEvent)
            .where(
                ActivityEvent.repo_id == run.repo_id,
                ActivityEvent.occurred_at >= target.period_start,
                ActivityEvent.occurred_at < target.period_end,
            )
            .order_by(ActivityEvent.occurred_at, ActivityEvent.id)
            .limit(10001)
        ).all()
        truncated = len(events) > 10000
        events = events[:10000]
        counts = dict(sorted(Counter(e.kind for e in events).items()))
        facts = [
            {
                "source_event_id": e.source_event_id,
                "kind": e.kind,
                "occurred_at": e.occurred_at.isoformat(),
                "subject_number": e.subject_number,
            }
            for e in events
        ]
        evidence = capture(
            db,
            settings,
            repo_id=run.repo_id,
            source_type="activity_snapshot",
            content=json.dumps({"facts": facts, "counts": counts}, ensure_ascii=False),
            locator=target.model_dump(mode="json"),
        )
        db.add(RunEvidence(run_id=run.id, evidence_id=evidence.id, usage="cited"))
        actor = db.get(Conversation, run.conversation_id).owner_id
        details = {
            **target.model_dump(mode="json"),
            "counts": counts,
            "event_count": len(events),
            "evidence_id": str(evidence.id),
            "truncated": truncated,
        }
        result = AnalysisResult(
            kind="weekly_report",
            summary=f"所选时间范围内已同步 {len(events)} 条活动事件。统计按事件发生时间归属，结束时间不包含在内。",
            verdict="not_applicable",
            coverage="partial",
            freshness="unknown",
            scope=RepoScope(repo_id=run.repo_id, actor_id=actor),
            details=details,
            limitations=[
                "统计仅涵盖已同步的 GitHub 活动；GitHub Events 的保留窗口及分页上限不能证明全量覆盖。"
            ]
            + (["事件超过 10000 条，报告已截断"] if truncated else []),
        ).model_dump(mode="json")
        db.add(
            WeeklyReport(
                repo_id=run.repo_id,
                run_id=run.id,
                period_start=target.period_start,
                period_end=target.period_end,
                timezone=target.timezone,
                content=result,
            )
        )
        run_service.finalize_run(
            db,
            run,
            status="completed",
            result=result,
            assistant_text=run_service.render_assistant_text(result),
            job_id=getattr(job, "id", None),
            job_epoch=getattr(job, "lease_epoch", None),
        )
        return {"status": run.status}
