"""Version-bound approval and one-shot external delivery. No automatic write retries."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

import httpx
from pydantic import Field, model_validator
from sqlalchemy import select

from app.core.errors import DomainError
from app.domain.contracts import Contract
from app.integrations.github.client import GitHubClient
from app.jobs.queue import LeaseLost, enqueue, renew
from app.persistence.models import (
    ActionDraft,
    ActionExecution,
    ActionVersion,
    Job,
    RepoMembership,
    Repository,
)
from app.services.auth_service import as_utc
from app.services.memory_service import audit


class ActionInput(Contract):
    action: Literal["create_issue_comment", "add_labels", "create_issue", "close_issue"]
    number: int | None = Field(default=None, ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=256)
    body: str | None = Field(default=None, min_length=1, max_length=10000)
    labels: list[str] = Field(default_factory=list, max_length=20)
    state_reason: Literal["completed", "not_planned"] = "completed"
    expected_head_sha: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")

    @model_validator(mode="after")
    def shape(self):
        if self.action != "create_issue" and self.number is None:
            raise ValueError("需要目标编号")
        if (
            self.action in {"create_issue", "create_issue_comment"}
            and not (self.body or "").strip()
        ):
            raise ValueError("正文不能为空")
        if self.action == "create_issue" and not (self.title or "").strip():
            raise ValueError("标题不能为空")
        if self.action == "add_labels" and not self.labels:
            raise ValueError("标签不能为空")
        if any(not label.strip() or len(label) > 50 for label in self.labels):
            raise ValueError("标签无效")
        return self


def fingerprint(action, payload, preconditions):
    return hashlib.sha256(
        json.dumps(
            [action, payload, preconditions],
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def validate_remote(client, repo, body):
    prefix = f"/repos/{repo.full_name}"
    if body.action == "create_issue":
        raw = client.get(prefix).data
        if not raw.get("has_issues"):
            raise DomainError("ACTION_PRECONDITION", "仓库未启用 Issues", 409)
        preconditions = {"has_issues": True}
    else:
        raw = client.get(f"{prefix}/issues/{body.number}").data
        if body.action == "close_issue" and (raw.get("pull_request") or raw.get("state") != "open"):
            raise DomainError("ACTION_PRECONDITION", "只能关闭处于 open 状态的 Issue", 409)
        preconditions = {"state": raw.get("state"), "updated_at": raw.get("updated_at")}
        if body.expected_head_sha:
            pr = client.get(f"{prefix}/pulls/{body.number}").data
            if pr.get("head", {}).get("sha") != body.expected_head_sha:
                raise DomainError("ACTION_STALE", "PR head 已变化", 409)
            preconditions["head_sha"] = body.expected_head_sha
    for label in body.labels:
        from urllib.parse import quote

        client.get(f"{prefix}/labels/{quote(label, safe='')}")
    return preconditions


def create_draft(db, settings, repo, actor_id, body, *, client=None, draft=None, draft_id=None):
    if repo.state != "active":
        raise DomainError("REPO_DISABLED", "仓库已禁用", 409)
    if draft:
        draft = db.scalar(select(ActionDraft).where(ActionDraft.id == draft.id).with_for_update())
        if draft.state not in {"pending", "approved"} or db.scalar(
            select(ActionExecution).where(ActionExecution.draft_id == draft.id)
        ):
            raise DomainError("STATE_CONFLICT", "此草稿不能再修改", 409)
    client = client or GitHubClient(settings, settings.github_read_token.get_secret_value())
    preconditions = validate_remote(client, repo, body)
    row = draft or ActionDraft(
        id=draft_id or uuid4(), repo_id=repo.id, actor_id=actor_id, version=0
    )
    row.version += 1
    row.action = body.action
    payload = body.model_dump(exclude_none=True)
    if body.action in {"create_issue", "create_issue_comment"}:
        payload["body"] = body.body + f"\n\n<!-- devflow-action:{row.id}:v{row.version} -->"
    row.payload, row.preconditions = payload, preconditions
    row.payload_hash = fingerprint(row.action, payload, preconditions)
    row.state, row.approved_by, row.approved_hash = "pending", None, None
    row.expires_at = datetime.now(UTC) + timedelta(hours=24)
    db.add(row)
    db.flush()
    db.add(
        ActionVersion(
            draft_id=row.id,
            version=row.version,
            payload={"action": row.action, "payload": payload, "preconditions": preconditions},
            payload_hash=row.payload_hash,
        )
    )
    audit(
        db,
        repo.id,
        actor_id,
        row.id,
        "action.revised" if draft else "action.created",
        version=row.version,
        payload_hash=row.payload_hash,
    )
    db.commit()
    return row


def approve(db, draft, actor_id, version, payload_hash):
    row = db.scalar(select(ActionDraft).where(ActionDraft.id == draft.id).with_for_update())
    if (
        row.state != "pending"
        or row.version != version
        or row.payload_hash != payload_hash
        or as_utc(row.expires_at) <= datetime.now(UTC)
    ):
        raise DomainError("APPROVAL_CONFLICT", "草稿版本、内容或有效期已变化，请重新查看", 409)
    row.state, row.approved_by, row.approved_hash = "approved", actor_id, payload_hash
    audit(
        db,
        row.repo_id,
        actor_id,
        row.id,
        "action.approved",
        version=version,
        payload_hash=payload_hash,
    )
    db.commit()


def enabled(settings, repo, draft):
    if (
        repo.state != "active"
        or draft.action not in settings.enabled_actions
        or repo.full_name not in settings.write_repo_allowlist
        or not settings.github_write_token.get_secret_value()
    ):
        raise DomainError("ACTION_DISABLED", "写操作、仓库白名单或写凭据未启用", 403)


def execute(db, settings, repo, draft, actor_id):
    row = db.scalar(select(ActionDraft).where(ActionDraft.id == draft.id).with_for_update())
    existing = db.scalar(select(ActionExecution).where(ActionExecution.draft_id == row.id))
    if existing:
        return existing
    enabled(settings, repo, row)
    if (
        row.state != "approved"
        or row.approved_hash != fingerprint(row.action, row.payload, row.preconditions)
        or as_utc(row.expires_at) <= datetime.now(UTC)
    ):
        raise DomainError("APPROVAL_REQUIRED", "需要批准当前有效版本", 409)
    execution = ActionExecution(draft_id=row.id)
    db.add(execution)
    db.flush()
    job = enqueue(db, kind="action", entity_id=execution.id, max_attempts=1)
    execution.job_id = job.id
    audit(db, row.repo_id, actor_id, row.id, "action.queued", execution_id=str(execution.id))
    db.commit()
    return execution


def revoke(db, draft, actor_id):
    row = db.scalar(select(ActionDraft).where(ActionDraft.id == draft.id).with_for_update())
    execution = db.scalar(
        select(ActionExecution).where(ActionExecution.draft_id == row.id).with_for_update()
    )
    if execution:
        job = db.scalar(select(Job).where(Job.id == execution.job_id).with_for_update())
        if job.status not in {"queued", "retry_wait"}:
            raise DomainError("ALREADY_DISPATCHED", "请求已领取，请核查远端结果", 409)
        job.status, execution.state = "cancelled", "cancelled"
    elif row.state not in {"pending", "approved"}:
        raise DomainError("STATE_CONFLICT", "当前状态无法撤销", 409)
    row.state = "rejected" if row.state == "pending" else "revoked"
    audit(db, row.repo_id, actor_id, row.id, "action." + row.state)
    db.commit()


def send_once(settings, repo, draft, *, transport=None):
    p = draft.payload
    root = f"/repos/{repo.full_name}/issues"
    route = root if draft.action == "create_issue" else f"{root}/{p['number']}"
    method = "POST"
    if draft.action == "create_issue_comment":
        route += "/comments"
        payload = {"body": p["body"]}
    elif draft.action == "add_labels":
        route += "/labels"
        payload = {"labels": p["labels"]}
    elif draft.action == "create_issue":
        payload = {key: p[key] for key in ("title", "body", "labels")}
    else:
        method, payload = "PATCH", {"state": "closed", "state_reason": p["state_reason"]}
    try:
        with httpx.Client(
            timeout=20,
            follow_redirects=False,
            trust_env=settings.github_trust_env,
            proxy=settings.github_api_proxy or None,
            transport=transport,
        ) as client:
            response = client.request(
                method,
                settings.github_api_base + route,
                json=payload,
                headers={
                    "Authorization": "Bearer " + settings.github_write_token.get_secret_value(),
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        if 200 <= response.status_code < 300:
            data = response.json()
            return "succeeded", {
                "remote_id": data.get("id") if isinstance(data, dict) else None,
                "url": data.get("html_url") if isinstance(data, dict) else None,
                "http_status": response.status_code,
            }
        state = (
            "failed" if response.status_code in {400, 401, 403, 404, 410, 422, 429} else "unknown"
        )
        return state, {"http_status": response.status_code}
    except (httpx.HTTPError, ValueError):
        return "unknown", {"code": "WRITE_OUTCOME_UNKNOWN"}


def handle_action(factory, settings, job, worker_id, *, client_factory=None, sender=None):
    if not renew(
        factory,
        job_id=job.id,
        worker_id=worker_id,
        epoch=job.lease_epoch,
        lease_seconds=settings.job_lease_seconds,
    ):
        raise LeaseLost()
    with factory() as db:
        execution = db.scalar(
            select(ActionExecution).where(ActionExecution.id == job.entity_id).with_for_update()
        )
        draft = db.scalar(
            select(ActionDraft).where(ActionDraft.id == execution.draft_id).with_for_update()
        )
        if execution.state != "queued":
            return {"status": execution.state}
        repo = db.get(Repository, draft.repo_id)
        try:
            enabled(settings, repo, draft)
            membership = db.scalar(
                select(RepoMembership).where(
                    RepoMembership.repo_id == repo.id, RepoMembership.user_id == draft.approved_by
                )
            )
            if not membership or membership.role not in {"maintainer", "admin"}:
                raise DomainError("FORBIDDEN", "批准人的仓库权限已失效", 403)
            if (
                draft.state != "approved"
                or draft.approved_hash
                != fingerprint(draft.action, draft.payload, draft.preconditions)
                or as_utc(draft.expires_at) <= datetime.now(UTC)
            ):
                raise DomainError("ACTION_STALE", "批准已失效", 409)
            client = (
                client_factory(settings, settings.github_write_token.get_secret_value())
                if client_factory
                else GitHubClient(settings, settings.github_write_token.get_secret_value())
            )
            me = client.get("/user").data["login"]
            preconditions = validate_remote(client, repo, ActionInput.model_validate(draft.payload))
            if preconditions != draft.preconditions:
                raise DomainError("ACTION_STALE", "远端前置条件已改变", 409)
        except DomainError as error:
            draft.state = (
                "stale" if error.code in {"ACTION_STALE", "ACTION_PRECONDITION"} else "failed"
            )
            execution.state, execution.result = "failed", {"code": error.code}
            audit(
                db, repo.id, draft.approved_by, draft.id, "action.preflight_failed", code=error.code
            )
            db.commit()
            return {"status": draft.state}
        # Durable sending marker precedes the one and only write attempt.
        if not renew(
            factory,
            job_id=job.id,
            worker_id=worker_id,
            epoch=job.lease_epoch,
            lease_seconds=settings.job_lease_seconds,
        ):
            raise LeaseLost()
        draft.state = execution.state = "executing"
        execution.started_at = datetime.now(UTC)
        execution.result = {"expected_author": me, "attempt_id": str(execution.id)}
        db.commit()
        state, result = (sender or send_once)(settings, repo, draft)
        execution.state = draft.state = state
        execution.result = {**execution.result, **result}
        audit(
            db,
            repo.id,
            draft.approved_by,
            draft.id,
            "action." + state,
            execution_id=str(execution.id),
            **result,
        )
        db.commit()
        return {"status": state}


def reconcile(db, settings, repo, draft, actor_id, *, client=None):
    execution = db.scalar(
        select(ActionExecution).where(ActionExecution.draft_id == draft.id).with_for_update()
    )
    if not execution or execution.state != "unknown":
        raise DomainError("STATE_CONFLICT", "只允许核查 unknown 操作", 409)
    client = client or GitHubClient(settings, settings.github_write_token.get_secret_value())
    p = draft.payload
    path = f"/repos/{repo.full_name}/issues"
    matched = None
    externally = False
    if draft.action in {"create_issue_comment", "create_issue"}:
        marker = f"<!-- devflow-action:{draft.id}:v{draft.version} -->"
        path = path + f"/{p['number']}/comments" if draft.action == "create_issue_comment" else path
        page = client.get(
            path, {"per_page": 100, "state": "all", "sort": "created", "direction": "desc"}
        )
        author = (execution.result or {}).get("expected_author")
        for row in page.data:
            created = datetime.fromisoformat(row.get("created_at", "1970-01-01T00:00:00Z"))
            if (
                marker in (row.get("body") or "")
                and author
                and (row.get("user") or {}).get("login") == author
                and execution.started_at
                and as_utc(execution.started_at) - timedelta(minutes=1)
                <= created
                <= as_utc(execution.started_at) + timedelta(minutes=10)
            ):
                matched = {"remote_id": row.get("id"), "url": row.get("html_url")}
                break
    else:
        raw = client.get(path + f"/{p['number']}").data
        satisfied = (
            raw.get("state") == "closed"
            if draft.action == "close_issue"
            else set(p["labels"]) <= {r["name"] for r in raw.get("labels", [])}
        )
        if satisfied:
            matched, externally = {"remote_id": raw.get("id"), "url": raw.get("html_url")}, True
    if matched:
        execution.state = draft.state = "succeeded"
        execution.result = {
            **(execution.result or {}),
            **matched,
            "satisfied_externally": externally,
        }
    audit(
        db,
        repo.id,
        actor_id,
        draft.id,
        "action.reconciled",
        matched=bool(matched),
        satisfied_externally=externally,
    )
    db.commit()
    return execution
