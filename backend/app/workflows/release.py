"""LangGraph orchestration with application-owned transactional checkpoints.

The DB task journal is authoritative. Restart replays only orchestration;
completed child runs are reused and interrupted model calls remain charged.
No external mutation is reachable from this graph.
"""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from sqlalchemy import func, select

from app.core.errors import DomainError
from app.domain.contracts import AnalysisResult, RepoScope
from app.ingestion.github_sync import create_read_client
from app.integrations.github.credentials import resolve_read_token
from app.integrations.github.read_adapter import GitHubRead
from app.jobs.queue import LeaseLost, renew
from app.persistence.models import (
    Conversation,
    Repository,
    Run,
    RunEvidence,
    TaskRun,
    WorkflowCheckpoint,
)
from app.services import run_service
from app.workflows.contracts import PlanTask, ReleaseTarget


class GraphState(TypedDict, total=False):
    plan: list[dict]
    tasks: list[dict]
    observation: dict
    result: dict


def _task_confidence(result: dict, evidence_count: int) -> float:
    """Confidence shown to the user, derived from verifiable signals only.

    It is a policy score, not a model self-report: coverage, freshness, evidence
    volume and explicit evidence gaps move it; missing results score zero.
    """
    if not result:
        return 0.0
    score = 0.45
    if result.get("coverage") == "complete":
        score += 0.2
    if result.get("freshness") == "fresh":
        score += 0.1
    score += min(0.2, evidence_count * 0.01)
    if result.get("verdict") == "insufficient_evidence":
        score -= 0.15
    return round(max(0.05, min(0.95, score)), 2)


def synthesize(
    tasks,
    *,
    actor_id,
    repo_id,
    head_sha,
    head_fresh,
    policy,
    observation: dict | None = None,
):
    observation = observation or {}
    evidence_counts = observation.get("evidence_counts") or {}
    results = [task.get("result") or {} for task in tasks]
    failures = [task["key"] for task in tasks if task["required"] and task["state"] != "completed"]
    reviews = [r for r in results if r.get("kind") == "pr_review"]
    ci = [r for r in results if r.get("kind") == "ci_debug"]
    blockers = any(r.get("verdict") == "hold" for r in reviews) or any(
        (r.get("details") or {}).get("conclusion") in {"failure", "timed_out", "action_required"}
        for r in ci
    )
    ci_passed = bool(ci) and all(
        (r.get("details") or {}).get("status") == "completed"
        and (r.get("details") or {}).get("conclusion") == "success"
        for r in ci
    )
    complete = bool(reviews and ci_passed and policy and not failures and head_fresh) and all(
        r.get("coverage") == "complete" and r.get("freshness") == "fresh" for r in results
    )
    verdict = (
        "hold"
        if blockers
        else "recommend_release"
        if complete and all(r.get("verdict") == "recommend_merge" for r in reviews)
        else "insufficient_evidence"
    )
    agents = []
    warnings: list[str] = []
    pending: list[str] = []
    for task in tasks:
        result = task.get("result") or {}
        key = task.get("key", "task")
        count = int(evidence_counts.get(key, 0))
        confidence = _task_confidence(result, count)
        agent_warnings = []
        if task["state"] != "completed":
            agent_warnings.append("任务未完成")
        elif count == 0:
            agent_warnings.append("没有返回明确证据或引用")
        if result and confidence < 0.5:
            agent_warnings.append("置信度偏低")
        if task.get("agent") == "issue_triage" and not (
            (result.get("details") or {}).get("owner_candidates") or []
        ):
            agent_warnings.append("负责人建议为空，或不在已配置团队成员中")
        for message in agent_warnings:
            warnings.append(f"{key}: {message}")
            pending.append(f"{key}: {message}")
        agents.append(
            {
                "key": key,
                "agent": task.get("agent"),
                "state": task["state"],
                "summary": result.get("summary"),
                "verdict": result.get("verdict"),
                "coverage": result.get("coverage"),
                "freshness": result.get("freshness"),
                "evidence_count": count,
                "confidence": confidence,
                "warnings": agent_warnings,
            }
        )
    next_actions = [
        action for result in results for action in (result.get("next_actions") or [])
    ][:8]
    missing_evidence = [
        item for result in results for item in (result.get("missing_evidence") or [])
    ][:8]
    return AnalysisResult(
        kind="release_check",
        summary="发布检查完成。"
        if complete
        else "发布检查已汇总；存在证据或策略缺口，不能给出完整放行建议。",
        verdict=verdict,
        coverage="complete" if complete else "partial",
        freshness="fresh" if head_fresh else "stale",
        findings=[f for r in results for f in r.get("findings", [])],
        hypotheses=[h for r in results for h in r.get("hypotheses", [])],
        missing_evidence=missing_evidence,
        next_actions=next_actions,
        limitations=([] if policy else ["仓库尚未配置 required_checks 发布策略"])
        + (["固定 head 已变化"] if not head_fresh else [])
        + (["必需任务未完成：" + ", ".join(failures)] if failures else [])
        + [limit for r in results for limit in r.get("limitations", [])],
        scope=RepoScope(actor_id=actor_id, repo_id=repo_id),
        details={
            "head_sha": head_sha,
            "tasks": tasks,
            "observer": {
                "failed_required": failures,
                "policy_configured": bool(policy),
                "agents": agents,
                "warnings": warnings,
                "pending": pending,
                "followup_rounds": observation.get("followup_rounds", 0),
            },
        },
    ).model_dump(mode="json")


def execute_release(factory, settings, job, worker_id, **adapters):
    from app.jobs.handlers.analyze import handle_analyze

    with factory() as db:
        run = db.get(Run, job.entity_id)
        if run.status == "recovering" and any(
            run.config_snapshot.get(key) != run_service.config_snapshot(settings, run.kind).get(key)
            for key in ("model", "provider", "toolset_version", "workflow_token_budget")
        ):
            run_service.finalize_run(
                db,
                run,
                status="failed",
                error={
                    "code": "CONFIG_CHANGED",
                    "message": "配置已改变，不能按旧工作流恢复；请新建分析",
                },
            )
            return {"status": "failed"}
        repo = db.get(Repository, run.repo_id)
        conversation = db.get(Conversation, run.conversation_id)
        if repo.state != "active":
            run_service.finalize_run(
                db, run, status="failed", error={"code": "REPO_DISABLED", "message": "仓库已禁用"}
            )
            return {"status": "failed"}
        run_service.mark_running(db, run)
        root_id, repo_id, actor_id = run.id, repo.id, conversation.owner_id
        target = ReleaseTarget.model_validate(
            {k: v for k, v in run.target.items() if k != "head_sha"}
        )
        started = (
            run.started_at.replace(tzinfo=UTC) if not run.started_at.tzinfo else run.started_at
        )
        deadline = started.timestamp() + settings.workflow_timeout_seconds
        token = resolve_read_token(settings, repo.credential_ref)
        maker = adapters.get("client_factory") or create_read_client
        read = GitHubRead(maker(settings, token), repo.full_name)

    def check():
        if run_service.cancel_requested(factory, root_id):
            raise DomainError("CANCELLED", "工作流已取消", 409)
        if datetime.now(UTC).timestamp() >= deadline:
            with factory() as db:
                row = db.get(Run, root_id)
                row.cancel_requested_at = datetime.now(UTC)
                db.commit()
            raise DomainError("WORKFLOW_TIMEOUT", "工作流已超时", 408)
        if not renew(
            factory,
            job_id=job.id,
            worker_id=worker_id,
            epoch=job.lease_epoch,
            lease_seconds=settings.job_lease_seconds,
        ):
            raise LeaseLost()

    def checkpoint(node, update):
        with factory() as db:
            row = db.get(WorkflowCheckpoint, root_id)
            if row is None:
                row = WorkflowCheckpoint(run_id=root_id, state={})
                db.add(row)
            row.state = {**row.state, **update, "node": node}
            row.updated_at = datetime.now(UTC)
            run_service.emit(db, db.get(Run, root_id), "workflow.checkpoint", {"node": node})
            db.commit()
        return update

    def planner(state):
        check()
        with factory() as db:
            rows = db.scalars(
                select(TaskRun).where(TaskRun.run_id == root_id).order_by(TaskRun.key)
            ).all()
            if rows:
                return {"plan": [r.specification for r in rows]}
        raw = read.get_pr_live(target.pr_number)
        head = (raw.get("head") or {}).get("sha")
        if not head:
            raise DomainError("HEAD_MISSING", "无法固定 PR 提交", 409)
        tasks = target.tasks or [
            PlanTask(key="review", agent="pr_review", target={"pr_number": target.pr_number})
        ]
        selected_ci = (
            {"ci_run_id": target.ci_run_id, "attempt": target.attempt} if target.ci_run_id else None
        )
        if not selected_ci:
            try:
                ci_lookup = read.ci_for_head(
                    head, max_pages=min(3, settings.sync_max_pages), check_stop=check
                )
                matches = ci_lookup["items"]
                if matches:
                    latest = max(
                        matches,
                        key=lambda item: (item.get("created_at", ""), item.get("run_attempt", 1)),
                    )
                    selected_ci = {
                        "ci_run_id": latest["id"],
                        "attempt": latest.get("run_attempt", 1),
                    }
            except DomainError as error:
                ci_lookup = {
                    "head_sha": head,
                    "error_code": error.code,
                    "message": error.message,
                    "remedy": (error.details or {}).get("remedy"),
                    "has_more": True,
                }
            with factory() as db:
                parent = db.get(Run, root_id)
                parent.config_snapshot = {**parent.config_snapshot, "ci_lookup": ci_lookup}
                db.commit()
        if selected_ci and not any(t.agent == "ci_debug" for t in tasks):
            tasks = tasks + [PlanTask(key="ci", agent="ci_debug", target=selected_ci)]
        if not any(t.agent == "pr_review" and t.required for t in tasks):
            raise DomainError("INVALID_PLAN", "发布检查需要必需的 PR Review 节点", 400)
        if len(tasks) > 8:
            raise DomainError("INVALID_PLAN", "计划最多 8 个任务", 400)
        with factory() as db:
            parent = db.get(Run, root_id)
            parent.target = {**parent.target, "head_sha": head}
            for task in tasks:
                clean = run_service.validate_target(task.agent, task.target)
                if task.agent == "pr_review" and clean["pr_number"] != target.pr_number:
                    raise DomainError("INVALID_PLAN", "PR 节点必须匹配发布目标", 400)
                spec = task.model_dump(mode="json")
                spec["target"] = {**clean, "expected_head_sha": head}
                db.add(TaskRun(run_id=root_id, key=task.key, agent=task.agent, specification=spec))
            db.commit()
        return checkpoint("planner", {"plan": [t.model_dump(mode="json") for t in tasks]})

    def child(task_id):
        with factory() as db:
            task = db.get(TaskRun, task_id)
            parent = db.get(Run, root_id)
            previous = db.get(Run, task.child_run_id) if task.child_run_id else None
            if previous and previous.status in run_service.TERMINAL_RUN_STATUSES:
                task.state, task.result = previous.status, previous.result
                db.commit()
                return
            if previous:
                run_service.finalize_run(
                    db,
                    previous,
                    status="failed",
                    error={"code": "WORKER_INTERRUPTED", "message": "节点调用中断"},
                )
            child_run = Run(
                repo_id=repo_id,
                parent_run_id=root_id,
                kind=task.agent,
                status="queued",
                target=task.specification["target"],
                context_through_seq=parent.context_through_seq,
                config_snapshot=parent.config_snapshot,
            )
            db.add(child_run)
            db.flush()
            task.child_run_id, task.state = child_run.id, "running"
            db.commit()
            child_id = child_run.id
        fake_job = SimpleNamespace(id=job.id, entity_id=child_id, lease_epoch=job.lease_epoch)
        try:
            handle_analyze(factory, settings, fake_job, worker_id, **adapters)
        finally:
            with factory() as db:
                task, row = db.get(TaskRun, task_id), db.get(Run, child_id)
                task.state, task.result = row.status, row.result
                db.commit()

    def scheduler(state):
        # Parent event stream has one writer; child agents own their separate streams.
        with ThreadPoolExecutor(max_workers=settings.workflow_parallelism) as pool:
            running = {}
            while True:
                check()
                with factory() as db:
                    rows = db.scalars(select(TaskRun).where(TaskRun.run_id == root_id)).all()
                    terminal = {*run_service.TERMINAL_RUN_STATUSES, "skipped"}
                    done = {r.key for r in rows if r.state in terminal}
                    failed = {
                        r.key
                        for r in rows
                        if r.state in {"failed", "cancelled", "timed_out", "skipped"}
                    }
                    skipped_now = False
                    for task in rows:
                        if task.id in running.values() or task.state in terminal:
                            continue
                        if set(task.specification["depends_on"]) & failed:
                            task.state = "skipped"
                            skipped_now = True
                            task.result = {"skipped": True, "reason": "dependency_failed"}
                            db.commit()
                            continue
                        if (
                            set(task.specification["depends_on"]) <= done
                            and len(running) < settings.workflow_parallelism
                        ):
                            running[pool.submit(child, task.id)] = task.id
                    if not running:
                        if skipped_now:
                            continue
                        break
                finished, _ = wait(running, timeout=1, return_when=FIRST_COMPLETED)
                for future in finished:
                    del running[future]
                    try:
                        future.result()
                    except DomainError:
                        pass  # failed node is recorded; Observer evaluates requiredness
            with factory() as db:
                tasks = [
                    {
                        "key": r.key,
                        "agent": r.agent,
                        "state": r.state,
                        "required": r.specification["required"],
                        "child_run_id": str(r.child_run_id),
                        "result": r.result,
                    }
                    for r in db.scalars(
                        select(TaskRun).where(TaskRun.run_id == root_id).order_by(TaskRun.key)
                    )
                ]
        return checkpoint("scheduler", {"tasks": tasks})

    def observer(state):
        check()
        reviews = [
            t
            for t in state["tasks"]
            if t["agent"] == "pr_review"
            and (t.get("result") or {}).get("verdict") == "recommend_merge"
        ]
        failing_ci = [
            t
            for t in state["tasks"]
            if t["agent"] == "ci_debug"
            and ((t.get("result") or {}).get("details") or {}).get("conclusion")
            in {"failure", "timed_out"}
        ]
        followup = False
        with factory() as db:
            existing = db.scalar(
                select(TaskRun).where(TaskRun.run_id == root_id, TaskRun.key == "observer_ci")
            )
            count = len(state["tasks"])
            if target.observer_rounds and reviews and failing_ci and not existing and count < 8:
                source = db.scalar(
                    select(TaskRun).where(
                        TaskRun.run_id == root_id, TaskRun.key == failing_ci[0]["key"]
                    )
                )
                spec = {
                    **source.specification,
                    "key": "observer_ci",
                    "depends_on": [],
                    "required": True,
                }
                db.add(
                    TaskRun(
                        run_id=root_id,
                        key="observer_ci",
                        agent="ci_debug",
                        specification=spec,
                        plan_version=2,
                    )
                )
                db.commit()
                followup = True
        return checkpoint(
            "observer",
            {
                "observation": {
                    "failed_required": [
                        t["key"]
                        for t in state["tasks"]
                        if t["required"] and t["state"] != "completed"
                    ],
                    "followup_rounds": 1 if followup or existing else 0,
                    "followup": followup,
                }
            },
        )

    def synthesis(state):
        check()
        with factory() as db:
            parent = db.get(Run, root_id)
            head = parent.target["head_sha"]
            fresh = (read.get_pr_live(target.pr_number).get("head") or {}).get("sha") == head
            child_ids = db.scalars(
                select(TaskRun.child_run_id).where(TaskRun.run_id == root_id)
            ).all()
            counted = dict(
                db.execute(
                    select(
                        RunEvidence.run_id,
                        func.count(func.distinct(RunEvidence.evidence_id)),
                    )
                    .where(RunEvidence.run_id.in_(child_ids))
                    .group_by(RunEvidence.run_id)
                ).all()
            )
            evidence_counts = {}
            for task in state["tasks"]:
                child = task.get("child_run_id")
                if child:
                    evidence_counts[task["key"]] = counted.get(UUID(child), 0)
            observation = {
                **(state.get("observation") or {}),
                "evidence_counts": evidence_counts,
            }
            result = synthesize(
                state["tasks"],
                actor_id=actor_id,
                repo_id=repo_id,
                head_sha=head,
                head_fresh=fresh,
                policy=(repo.config_json or {}).get("required_checks"),
                observation=observation,
            )
            existing = {
                (r.evidence_id, r.usage)
                for r in db.scalars(select(RunEvidence).where(RunEvidence.run_id == root_id))
            }
            child_ids = db.scalars(
                select(TaskRun.child_run_id).where(TaskRun.run_id == root_id)
            ).all()
            for binding in db.scalars(select(RunEvidence).where(RunEvidence.run_id.in_(child_ids))):
                key = (binding.evidence_id, binding.usage)
                if key not in existing:
                    db.add(RunEvidence(run_id=root_id, evidence_id=key[0], usage=key[1]))
                    existing.add(key)
            db.commit()
        return checkpoint("synthesis", {"result": result})

    graph = StateGraph(GraphState)
    for name, fn in [
        ("planner", planner),
        ("scheduler", scheduler),
        ("observer", observer),
        ("synthesis", synthesis),
    ]:
        graph.add_node(name, fn)
    for a, b in [
        (START, "planner"),
        ("planner", "scheduler"),
        ("scheduler", "observer"),
        ("synthesis", END),
    ]:
        graph.add_edge(a, b)
    graph.add_conditional_edges(
        "observer",
        lambda state: "scheduler" if state["observation"].get("followup") else "synthesis",
    )
    try:
        output = graph.compile().invoke({}, {"recursion_limit": 10})
        with factory() as db:
            run_service.finalize_run(
                db,
                db.get(Run, root_id),
                status="completed",
                job_id=job.id,
                job_epoch=job.lease_epoch,
                result=output["result"],
                assistant_text=run_service.render_assistant_text(output["result"]),
            )
    except DomainError as error:
        with factory() as db:
            status = (
                "cancelled"
                if error.code == "CANCELLED"
                else "timed_out"
                if error.code == "WORKFLOW_TIMEOUT"
                else "failed"
            )
            run_service.finalize_run(
                db,
                db.get(Run, root_id),
                status=status,
                job_id=job.id,
                job_epoch=job.lease_epoch,
                error={"code": error.code, "message": error.message},
            )
    with factory() as db:
        return {"status": db.get(Run, root_id).status}
