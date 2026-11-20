"""Issue triage and attempt-scoped CI diagnosis using the shared run lifecycle."""

import json
import re

from app.agents.pr_review import filter_evidence_refs
from app.agents.specialists import CIDraft, IssueDraft, prompt
from app.core.errors import DomainError
from app.domain.contracts import AnalysisResult, Contract, RepoScope
from app.domain.scope import ExecutionContext
from app.ingestion.ci_logs import parse_log
from app.tools.executor import TOOLSET_VERSION, ToolSpec, ok_result
from app.workspace.reader import read_file


class TargetInput(Contract):
    pass


async def get_analysis_target(deps, args):
    return ok_result(
        deps.scratch["target_data"],
        source="fixed_target",
        evidence_ids=deps.scratch["target_evidence_ids"],
        status="partial" if deps.scratch["limitations"] else "ok",
    )


def capture_json(execution, data, source_type, locator):
    return execution.evidence(
        source_type=source_type, content=json.dumps(data, ensure_ascii=False), locator=locator
    )


def prepare_issue(execution, read, workspace):
    target, repo = execution.run.target, execution.repo
    number = target["issue_number"]
    raw = read.get_issue_live(number)
    if "pull_request" in raw or raw.get("number") != number:
        raise DomainError("INVALID_TARGET", "指定对象不是目标 Issue", 400)
    data = {key: raw.get(key) for key in ("number", "title", "state", "updated_at", "html_url")}
    body = raw.get("body") or ""
    data["body"] = body[:10000]
    limitations = ["Issue 描述超过上限，已截断"] if len(body) > 10000 else []
    evidence_id = capture_json(
        execution, data, "issue", {"number": number, "updated_at": raw.get("updated_at")}
    )
    data["evidence_id"] = str(evidence_id)
    ids = [evidence_id]
    sha = None
    try:
        sha = execution.run.target.get("expected_head_sha") or (
            read.get_branch_head(repo.default_branch) if repo.default_branch else None
        )
    except DomainError:
        limitations.append("无法固定默认分支，代码取证不可用")
    owners, duplicates = {}, {}
    if sha:
        for path in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
            execution.check_stop()
            try:
                content = read_file(
                    execution.settings, workspace, repo, sha, path, start_line=1, end_line=400
                )
            except DomainError:
                continue
            owner_id = execution.evidence(
                source_type="code",
                content=content.text,
                locator={
                    "commit_sha": sha,
                    "path": path,
                    "line_start": content.line_start,
                    "line_end": content.line_end,
                },
            )
            ids.append(owner_id)
            for line in content.text.splitlines():
                line = line.split("#", 1)[0]
                for match in re.findall(r"(?<!\S)@([A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)?)", line):
                    owners[match] = str(owner_id)
            if content.truncated:
                limitations.append("CODEOWNERS 仅读取前 400 行")
            break
    data["owner_candidates_allowed"] = owners
    if not owners:
        limitations.append("没有可核查的 CODEOWNERS 候选，负责人建议保持为空")
    try:
        candidates = read.list_issues("all").items
        visible = []
        for item in [item for item in candidates if item.get("number") != number][:20]:
            candidate = {
                "number": item["number"],
                "title": item.get("title"),
                "body": (item.get("body") or "")[:800],
            }
            cid = capture_json(
                execution,
                candidate,
                "issue",
                {"number": item["number"], "updated_at": item.get("updated_at")},
            )
            duplicates[str(item["number"])] = str(cid)
            candidate["evidence_id"] = str(cid)
            visible.append(candidate)
            ids.append(cid)
        data["recent_issue_candidates"] = visible
        limitations.append("相似问题检索限定最近最多 20 个 Issue，不代表全仓库排重")
    except DomainError:
        limitations.append("历史 Issue 候选不可用")
    return data, ids, limitations, sha, owners, duplicates


def prepare_ci(execution, read):
    settings, target = execution.settings, execution.run.target
    run_id, attempt = target["ci_run_id"], target["attempt"]
    raw = read.get_ci_attempt(run_id, attempt)
    if target.get("expected_head_sha") and raw.get("head_sha") != target["expected_head_sha"]:
        raise DomainError("HEAD_CHANGED", "CI 不属于工作流固定提交", 409)
    if raw.get("id") != run_id or raw.get("run_attempt") != attempt:
        raise DomainError("INVALID_TARGET", "CI run/attempt 与目标不匹配", 400)
    data = {
        key: raw.get(key)
        for key in ("id", "run_attempt", "head_sha", "status", "conclusion", "name", "updated_at")
    }
    meta_id = capture_json(execution, data, "ci_run", {"ci_run_id": run_id, "attempt": attempt})
    ids, limitations, jobs, cursor = [meta_id], [], [], None
    for _ in range(settings.sync_max_pages):
        execution.check_stop()
        page = read.list_ci_jobs(str(run_id), attempt, cursor)
        jobs.extend(page.items)
        cursor = page.next_cursor
        if not cursor:
            break
    if cursor:
        limitations.append("CI job 列表达到分页上限")
    if any(
        job.get("run_id") not in {None, run_id} or job.get("run_attempt") not in {None, attempt}
        for job in jobs
    ):
        raise DomainError("INVALID_TARGET", "CI job 不属于目标运行", 400)
    if target.get("job_id"):
        jobs = [job for job in jobs if job.get("id") == target["job_id"]]
        if not jobs:
            raise DomainError("INVALID_TARGET", "指定 job 不在目标 attempt 的已读取范围内", 400)
    data["jobs"] = [
        {key: job.get(key) for key in ("id", "name", "status", "conclusion")} for job in jobs[:100]
    ]
    if len(jobs) > 100:
        limitations.append("job 展示只保留前 100 项")
    data["step_failures"] = [
        {
            "job_id": job["id"],
            "name": str(step.get("name", ""))[:200],
            "number": step.get("number"),
            "conclusion": step.get("conclusion"),
        }
        for job in jobs[:100]
        for step in job.get("steps", [])[:100]
        if step.get("conclusion") in {"failure", "timed_out", "action_required"}
    ][:100]
    jobs_id = capture_json(
        execution,
        {"jobs": data["jobs"], "step_failures": data["step_failures"]},
        "ci_jobs",
        {"ci_run_id": run_id, "attempt": attempt},
    )
    ids.append(jobs_id)
    data["jobs_evidence_id"] = str(jobs_id)
    data["error_blocks"] = []
    candidates = [
        job for job in jobs if job.get("conclusion") in {"failure", "timed_out", "action_required"}
    ]
    if len(candidates) > settings.max_ci_log_jobs:
        limitations.append("失败 job 超过日志采集上限，未覆盖全部 job")
    secrets = [
        settings.github_read_token.get_secret_value(),
        settings.github_write_token.get_secret_value(),
        settings.llm_api_key.get_secret_value(),
        settings.embedding_api_key.get_secret_value(),
    ]
    for job in candidates[: settings.max_ci_log_jobs]:
        execution.check_stop()
        try:
            raw_log, clipped = read.get_job_log(job["id"], settings.max_ci_log_bytes)
            parsed = parse_log(
                raw_log,
                truncated=clipped,
                max_blocks=settings.max_ci_error_blocks,
                known_secrets=secrets,
            )
            limitations.extend(f"job {job['id']}：{note}" for note in parsed["warnings"])
            if not parsed["blocks"]:
                limitations.append(f"job {job['id']} 没有可读日志片段")
            for block in parsed["blocks"]:
                locator = {
                    "ci_run_id": run_id,
                    "attempt": attempt,
                    "job_id": job["id"],
                    "line_start": block["line_start"],
                    "line_end": block["line_end"],
                    "commit_sha": raw.get("head_sha"),
                }
                eid = execution.evidence(
                    source_type="ci_log", content=block["text"], locator=locator
                )
                ids.append(eid)
                data["error_blocks"].append(
                    {
                        **block,
                        "job_id": job["id"],
                        "job_name": job.get("name"),
                        "evidence_id": str(eid),
                    }
                )
        except DomainError as error:
            remedy = (error.details or {}).get("remedy", "")
            limitations.append(f"job {job['id']}：[{error.code}] {error.message} {remedy}".strip())
            data.setdefault("log_diagnostics", []).append(
                {
                    "job_id": job["id"],
                    "code": error.code,
                    "message": error.message,
                    "remedy": remedy,
                }
            )
    if raw.get("status") != "completed":
        limitations.append("目标 CI 尚未完成，当前日志和结论可能变化")
    if raw.get("conclusion") == "failure" and not data["error_blocks"]:
        limitations.append("目标 CI 失败但缺少日志证据，不能判断根因")
    # Keep the model payload bounded even when multiple failure windows are large.
    while len(json.dumps(data, ensure_ascii=False).encode()) > 48000 and data["error_blocks"]:
        data["error_blocks"].pop()
        if "日志片段超过模型可见上限" not in limitations:
            limitations.append("日志片段超过模型可见上限")
    data["evidence_id"] = str(meta_id)
    return data, ids, limitations, raw.get("head_sha"), {}, {}


async def execute_specialist(
    db_factory,
    settings,
    *,
    run_id,
    job_id,
    job_epoch,
    worker_id,
    client_factory,
    model_factory,
    workspace_factory,
    vector_store_factory=None,
    embedding_factory=None,
):
    from app.jobs.handlers.analyze import (
        KNOWLEDGE_QA_TOOLS,
        _make_workspace,
        _model_maker,
        _run_flow,
        _start_execution,
    )

    execution, read, token = _start_execution(
        db_factory,
        settings,
        run_id=run_id,
        job_id=job_id,
        job_epoch=job_epoch,
        worker_id=worker_id,
        client_factory=client_factory,
    )
    try:
        workspace = _make_workspace(workspace_factory)(settings, token)
        kind = execution.run.kind
        execution.check_stop()
        prepared = (
            prepare_issue(execution, read, workspace)
            if kind == "issue_triage"
            else prepare_ci(execution, read)
        )
        data, ids, limitations, sha, owners, duplicates = prepared
        execution.run.target = {**execution.run.target, "head_sha": sha}
        execution.db.commit()
        user_message, _ = execution.load_history()
        messages, context_info = execution.assemble(
            system_prompt=prompt(kind, execution.repo.full_name),
            extra_user_messages=[
                {
                    "role": "user",
                    "content": user_message.content if user_message else "请分析指定目标",
                }
            ],
        )
        specs = [
            ToolSpec(
                "get_analysis_target",
                "读取本次固定目标、证据与采集边界。",
                TargetInput,
                get_analysis_target,
            )
        ]
        specs += [
            spec
            for spec in KNOWLEDGE_QA_TOOLS
            if spec.name in {"search_knowledge", "read_file", "search_code"}
        ]
        scratch = {
            "target_data": {**data, "limitations": limitations},
            "target_evidence_ids": ids,
            "limitations": limitations,
            "vector_store_factory": vector_store_factory,
            "embedding_factory": embedding_factory,
        }
        outcome = await _run_flow(
            execution,
            messages=messages,
            context_info=context_info,
            output_model=IssueDraft if kind == "issue_triage" else CIDraft,
            specs=specs,
            read=read,
            workspace=workspace,
            scratch=scratch,
            ctx=ExecutionContext(
                actor_id=execution.conversation.owner_id,
                repo_id=execution.repo.id,
                credential_ref=execution.repo.credential_ref,
                run_id=run_id,
                conversation_id=execution.conversation.id,
                head_sha=sha,
                prompt_version=f"{kind}.v1",
                toolset_version=TOOLSET_VERSION,
                lease_epoch=job_epoch,
            ),
            agent=kind,
            model_maker=_model_maker(model_factory),
        )
        if isinstance(outcome, dict):
            return outcome
        draft, notes = filter_evidence_refs(
            outcome.parsed, execution.collected_ids, execution.draft_ids
        )
        limitations.extend(notes)
        limitations.extend(draft.limitations)
        if kind == "issue_triage":
            for field, allowed in (
                ("owner_candidates", owners),
                ("duplicate_candidates", duplicates),
            ):
                candidates = getattr(draft, field)
                valid = [
                    candidate
                    for candidate in candidates
                    if candidate.value in allowed
                    and allowed[candidate.value] in {str(e) for e in candidate.evidence_ids}
                ]
                for candidate in valid:
                    candidate.evidence_ids = [
                        eid
                        for eid in candidate.evidence_ids
                        if str(eid) == allowed[candidate.value]
                    ]
                if len(valid) != len(candidates):
                    limitations.append(f"已移除无可核查依据的 {field}")
                setattr(draft, field, valid)
            draft.priority_evidence_ids = [
                eid for eid in draft.priority_evidence_ids if eid in execution.collected_ids
            ]
            if not draft.priority_evidence_ids:
                draft.priority = "unknown"
                draft.priority_reason = "缺少可核查的影响依据"
            details = draft.model_dump(
                mode="json",
                include={
                    "category",
                    "priority",
                    "priority_reason",
                    "priority_evidence_ids",
                    "complexity",
                    "reproduction",
                    "missing_fields",
                    "owner_candidates",
                    "duplicate_candidates",
                },
            )
        else:
            log_ids = {block["evidence_id"] for block in data["error_blocks"]}
            supported = any(
                str(eid) in log_ids
                for hypothesis in draft.hypotheses
                for eid in hypothesis.supporting_evidence_ids
            )
            details = {
                "log_diagnostics": data.get("log_diagnostics", []),
                "step_failures": data.get("step_failures", []),
                "run": data["id"],
                "attempt": data["run_attempt"],
                "jobs": data["jobs"],
                "status": data["status"],
                "conclusion": data["conclusion"],
                "error_blocks": data["error_blocks"],
                "cause_category": draft.cause_category if supported else "unknown",
                "verification_steps": draft.verification_steps,
                "fix_suggestions": draft.fix_suggestions,
            }
        for action in draft.next_actions:
            if action.owner_hint and action.owner_hint.lstrip("@") not in owners:
                action.owner_hint = None
        if kind == "issue_triage":
            details["action_items"] = [a.model_dump(mode="json") for a in draft.next_actions]
        freshness = "unknown"
        try:
            current = (
                read.get_issue_live(execution.run.target["issue_number"])
                if kind == "issue_triage"
                else read.get_ci_attempt(data["id"], data["run_attempt"])
            )
            freshness = "fresh" if current.get("updated_at") == data.get("updated_at") else "stale"
        except DomainError:
            limitations.append("无法复查目标新鲜度")
        if context_info["history_trimmed"]:
            limitations.append("早期对话已按预算裁剪")
        result = AnalysisResult(
            kind=kind,
            summary=draft.summary,
            verdict="not_applicable",
            coverage="partial" if limitations or draft.missing_evidence else "complete",
            freshness=freshness,
            findings=draft.findings,
            hypotheses=draft.hypotheses,
            missing_evidence=draft.missing_evidence,
            next_actions=draft.next_actions,
            limitations=limitations,
            details=details,
            scope=RepoScope(actor_id=execution.conversation.owner_id, repo_id=execution.repo.id),
        )
        return execution.finalize_success(
            result, stop_reason=None, outcome=outcome, context_info=context_info
        )
    except DomainError as error:
        if error.code == "CANCELLED":
            execution.cancel()
            return {"status": "cancelled"}
        execution.fail_run(error.code, error.message)
        return {"status": "failed"}
    finally:
        execution.close()
