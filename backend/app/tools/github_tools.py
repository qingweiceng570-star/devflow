"""Repository-data tools reading synchronized state (README 10.3).

These tools never trigger a full sync and never invent fresh data: every
response carries observed_at/freshness derived from the sync cursors, and
identity fields (repo, SHA) come from the execution context, not the model.
"""

import base64
import binascii
import json
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import Field
from sqlalchemy import func, select

from app.core.errors import DomainError
from app.domain.contracts import Contract, ToolResult
from app.persistence.models import CiJob, CiRun, Issue, PullRequest, SyncCursor
from app.storage.local import read_object, text_excerpt
from app.tools.executor import ToolDeps, db_freshness, ok_result

LIST_PAGE_SIZE = 30
SUMMARY_CI_LIMIT = 20
BODY_EXCERPT_CHARS = 3000


class RepoSummaryInput(Contract):
    window_days: int = Field(default=7, ge=1, le=90)
    ref: str | None = Field(default=None, max_length=200)


class ListIssuesInput(Contract):
    state: Literal["open", "closed", "all"] = "open"
    labels: list[str] = Field(default_factory=list, max_length=10)
    cursor: str | None = Field(default=None, max_length=200)


class GetIssueInput(Contract):
    issue_number: int = Field(ge=1)


class ListPullsInput(Contract):
    state: Literal["open", "closed", "all"] = "open"
    cursor: str | None = Field(default=None, max_length=200)


class GetPrInput(Contract):
    pr_number: int = Field(ge=1)


class GetPrDiffInput(Contract):
    pr_number: int = Field(ge=1)
    cursor: str | None = Field(default=None, max_length=200)


class GetCiRunsInput(Contract):
    pr_number: int | None = Field(default=None, ge=1)


class ReadFileInput(Contract):
    relative_path: str = Field(min_length=1, max_length=1024)
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class SearchCodeInput(Contract):
    query: str = Field(min_length=1, max_length=256)
    path_glob: str | None = Field(default=None, max_length=1024)


DIFF_PAGE_FILES = 8


async def get_pr_diff(deps: ToolDeps, args: GetPrDiffInput) -> ToolResult:
    """Serve the prepared, bounded diff scope page by page (README 9.2).

    Patches were fetched and captured once at run start; this tool never
    re-fetches the whole PR and never exceeds the prepared scope.
    """
    prepared = deps.scratch.get("pr_diff")
    if not prepared or prepared.get("pr_number") != args.pr_number:
        return ok_result(
            {"pr_number": args.pr_number, "available": False},
            source="prepared_diff",
            status="partial",
            warnings=["DIFF_NOT_PREPARED"],
        )
    start = _decode_cursor("diff", args.cursor) or 0
    files = prepared["files"]
    page = files[start : start + DIFF_PAGE_FILES]
    items = []
    for entry in page:
        item = {
            "path": entry["path"],
            "status": entry["status"],
            "additions": entry["additions"],
            "deletions": entry["deletions"],
            "evidence_id": str(entry["evidence_id"]) if entry["evidence_id"] else None,
        }
        if entry.get("patch"):
            item["patch"] = entry["patch"]
            item["patch_truncated"] = entry["patch_truncated"]
        else:
            item["patch"] = None
            item["note"] = "无可用补丁（二进制、过大或生成文件），可用 read_file 查看目标版本"
        items.append(item)
    next_cursor = (
        _encode_cursor("diff", start + len(page)) if start + len(page) < len(files) else None
    )
    data = {
        "pr_number": args.pr_number,
        "head_sha": prepared["head_sha"],
        "files": items,
        "position": f"{start + 1}-{start + len(page)} / {len(files)}",
        "coverage": prepared["coverage"],
        "unreviewed": prepared["unreviewed"],
    }
    return ok_result(
        data,
        source="prepared_diff",
        evidence_ids=[entry["evidence_id"] for entry in page if entry["evidence_id"]],
        next_cursor=next_cursor,
        truncated=next_cursor is not None,
        warnings=["UNREVIEWED_FILES"] if prepared["unreviewed"] else [],
    )


def _encode_cursor(kind: str, number: int) -> str:
    return base64.urlsafe_b64encode(f"{kind}:{number}".encode()).decode()


def _decode_cursor(kind: str, cursor: str | None) -> int | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        prefix, _, value = raw.partition(":")
        if prefix != kind:
            raise ValueError
        return int(value)
    except (ValueError, binascii.Error):
        raise DomainError("INVALID_TOOL_ARGUMENTS", "游标不合法", 400) from None


def _sync_freshness(db, repo_id, settings) -> tuple[str, str | None]:
    latest = db.scalar(
        select(func.max(SyncCursor.updated_at)).where(SyncCursor.repo_id == repo_id)
    )
    return db_freshness(latest, settings)


def _body_text(db, deps: ToolDeps, body_ref: str | None) -> tuple[str, bool]:
    if not body_ref:
        return "", False
    raw = read_object(deps.settings.storage_root, body_ref)
    return text_excerpt(raw.decode("utf-8", errors="replace"), limit=BODY_EXCERPT_CHARS)


async def get_repo_summary(deps: ToolDeps, args: RepoSummaryInput) -> ToolResult:
    since = datetime.now(UTC) - timedelta(days=args.window_days)
    with deps.db_factory() as db:
        repo_id = deps.repo.id
        counts = {
            "open_issues": db.scalar(
                select(func.count()).select_from(Issue).where(
                    Issue.repo_id == repo_id, Issue.state == "open"
                )
            ),
            "open_pulls": db.scalar(
                select(func.count()).select_from(PullRequest).where(
                    PullRequest.repo_id == repo_id, PullRequest.state == "open"
                )
            ),
            "merged_pulls_recent": db.scalar(
                select(func.count()).select_from(PullRequest).where(
                    PullRequest.repo_id == repo_id,
                    PullRequest.merged_at.is_not(None),
                    PullRequest.merged_at >= since,
                )
            ),
            "failing_ci_runs_recent": db.scalar(
                select(func.count()).select_from(CiRun).where(
                    CiRun.repo_id == repo_id,
                    CiRun.conclusion == "failure",
                    CiRun.source_created_at >= since,
                )
            ),
        }
        freshness, as_of = _sync_freshness(db, repo_id, deps.settings)
    return ok_result(
        {
            "counts": counts,
            "as_of": as_of,
            "window_days": args.window_days,
            "only_synced_scope": True,
            "note": "计数来自已同步范围内的数据，不代表远端全量",
        },
        source="synchronized_data",
        freshness=freshness,
    )


async def list_issues(deps: ToolDeps, args: ListIssuesInput) -> ToolResult:
    after = _decode_cursor("issue", args.cursor)
    with deps.db_factory() as db:
        query = select(Issue).where(Issue.repo_id == deps.repo.id)
        if args.state != "all":
            query = query.where(Issue.state == args.state)
        if after is not None:
            query = query.where(Issue.number < after)
        rows = db.scalars(query.order_by(Issue.number.desc()).limit(LIST_PAGE_SIZE + 1)).all()
        freshness, as_of = _sync_freshness(db, deps.repo.id, deps.settings)
    has_more = len(rows) > LIST_PAGE_SIZE
    rows = rows[:LIST_PAGE_SIZE]
    if args.labels:
        wanted = {label.lower() for label in args.labels}
        rows = [row for row in rows if wanted & {name.lower() for name in (row.labels or [])}]
    items = [
        {
            "number": row.number,
            "title": row.title[:300],
            "state": row.state,
            "author": row.author_login,
            "labels": row.labels or [],
            "updated_at": row.source_updated_at.isoformat() if row.source_updated_at else None,
        }
        for row in rows
    ]
    next_cursor = _encode_cursor("issue", rows[-1].number) if has_more and rows else None
    return ok_result(
        {"items": items, "as_of": as_of, "note": "标签过滤在分页内生效"},
        source="synchronized_data",
        freshness=freshness,
        next_cursor=next_cursor,
    )


async def get_issue(deps: ToolDeps, args: GetIssueInput) -> ToolResult:
    with deps.db_factory() as db:
        row = db.scalar(
            select(Issue).where(Issue.repo_id == deps.repo.id, Issue.number == args.issue_number)
        )
        if row is None:
            return ok_result(
                {"issue_number": args.issue_number, "found": False},
                source="synchronized_data",
                status="partial",
                warnings=["ISSUE_NOT_IN_SYNCED_SCOPE"],
            )
        body, truncated = _body_text(db, deps, row.body_ref)
        freshness, observed = _sync_freshness(db, deps.repo.id, deps.settings)
        evidence_id = deps.evidence(
            source_type="issue",
            content=body or f"(no body) issue #{row.number} {row.title}",
            locator={
                "issue_number": row.number,
                "external_id": row.external_id,
                "state": row.state,
            },
            source_url=row.html_url,
        )
        data = {
            "number": row.number,
            "title": row.title,
            "state": row.state,
            "author": row.author_login,
            "labels": row.labels or [],
            "body_excerpt": body,
            "body_truncated": truncated,
            "comments_count": row.comments_count,
            "html_url": row.html_url,
            "source_updated_at": row.source_updated_at.isoformat()
            if row.source_updated_at
            else None,
            "discussion_coverage": "body_only" if row.comments_count else "no_comments",
        }
    warnings = ["COMMENTS_NOT_SYNCED"] if row.comments_count else []
    return ok_result(
        data,
        source="synchronized_data",
        evidence_ids=[evidence_id],
        freshness=freshness,
        warnings=warnings,
    )


async def list_pull_requests(deps: ToolDeps, args: ListPullsInput) -> ToolResult:
    after = _decode_cursor("pull", args.cursor)
    with deps.db_factory() as db:
        query = select(PullRequest).where(PullRequest.repo_id == deps.repo.id)
        if args.state != "all":
            query = query.where(PullRequest.state == args.state)
        if after is not None:
            query = query.where(PullRequest.number < after)
        rows = db.scalars(
            query.order_by(PullRequest.number.desc()).limit(LIST_PAGE_SIZE + 1)
        ).all()
        freshness, as_of = _sync_freshness(db, deps.repo.id, deps.settings)
    has_more = len(rows) > LIST_PAGE_SIZE
    rows = rows[:LIST_PAGE_SIZE]
    items = [
        {
            "number": row.number,
            "title": row.title[:300],
            "state": row.state,
            "draft": row.draft,
            "merged": row.merged,
            "head_sha": row.head_sha,
            "updated_at": row.source_updated_at.isoformat() if row.source_updated_at else None,
        }
        for row in rows
    ]
    next_cursor = _encode_cursor("pull", rows[-1].number) if has_more and rows else None
    return ok_result(
        {"items": items, "as_of": as_of},
        source="synchronized_data",
        freshness=freshness,
        next_cursor=next_cursor,
    )


async def get_pr(deps: ToolDeps, args: GetPrInput) -> ToolResult:
    with deps.db_factory() as db:
        row = db.scalar(
            select(PullRequest).where(
                PullRequest.repo_id == deps.repo.id, PullRequest.number == args.pr_number
            )
        )
        if row is None:
            return ok_result(
                {"pr_number": args.pr_number, "found": False},
                source="synchronized_data",
                status="partial",
                warnings=["PR_NOT_IN_SYNCED_SCOPE"],
            )
        body, truncated = _body_text(db, deps, row.body_ref)
        freshness, observed = _sync_freshness(db, deps.repo.id, deps.settings)
        evidence_id = deps.evidence(
            source_type="pull_request",
            content=body or f"(no body) PR #{row.number} {row.title}",
            locator={
                "pr_number": row.number,
                "external_id": row.external_id,
                "head_sha": row.head_sha,
            },
            source_url=row.html_url,
        )
        data = {
            "number": row.number,
            "title": row.title,
            "state": row.state,
            "draft": row.draft,
            "merged": row.merged,
            "merged_at": row.merged_at.isoformat() if row.merged_at else None,
            "author": row.author_login,
            "labels": row.labels or [],
            "base_ref": row.base_ref,
            "base_sha": row.base_sha,
            "head_ref": row.head_ref,
            "head_sha": row.head_sha,
            "body_excerpt": body,
            "body_truncated": truncated,
            "html_url": row.html_url,
            "source_updated_at": row.source_updated_at.isoformat()
            if row.source_updated_at
            else None,
        }
    return ok_result(data, source="synchronized_data", evidence_ids=[evidence_id], freshness=freshness)


async def get_ci_runs(deps: ToolDeps, args: GetCiRunsInput) -> ToolResult:
    with deps.db_factory() as db:
        if args.pr_number is not None:
            pr = db.scalar(
                select(PullRequest).where(
                    PullRequest.repo_id == deps.repo.id, PullRequest.number == args.pr_number
                )
            )
            head_sha = pr.head_sha if pr else None
        else:
            head_sha = deps.ctx.head_sha
        if not head_sha:
            return ok_result(
                {"runs": [], "matched_head_sha": None},
                source="synchronized_data",
                status="partial",
                warnings=["NO_TARGET_SHA"],
            )
        runs = db.scalars(
            select(CiRun)
            .where(CiRun.repo_id == deps.repo.id, CiRun.head_sha == head_sha)
            .order_by(CiRun.source_created_at.desc())
            .limit(SUMMARY_CI_LIMIT)
        ).all()
        failing_jobs: dict[str, list[str]] = {}
        if runs:
            job_rows = db.scalars(
                select(CiJob).where(
                    CiJob.repo_id == deps.repo.id,
                    CiJob.ci_run_id.in_([run.id for run in runs]),
                    CiJob.conclusion == "failure",
                )
            ).all()
            for job in job_rows:
                failing_jobs.setdefault(str(job.ci_run_id), []).append(job.name[:200])
        freshness, observed = _sync_freshness(db, deps.repo.id, deps.settings)
    run_dicts = [
        {
            "run_id": run.external_id,
            "attempt": run.attempt,
            "workflow": run.workflow_name,
            "status": run.status,
            "conclusion": run.conclusion,
            "head_sha": run.head_sha,
            "html_url": run.html_url,
            "failing_jobs": failing_jobs.get(str(run.id), []),
            "started_at": run.run_started_at.isoformat() if run.run_started_at else None,
        }
        for run in runs
    ]
    evidence_id = None
    if runs:
        evidence_id = deps.evidence(
            source_type="ci_runs",
            content=json.dumps(run_dicts, ensure_ascii=False, indent=1),
            locator={"matched_head_sha": head_sha, "run_ids": [run.external_id for run in runs]},
        )
    data = {
        "runs": run_dicts,
        "matched_head_sha": head_sha,
        "note": "只包含与目标 head 匹配的已同步运行；其他 SHA 的成功不能说明当前版本通过",
    }
    return ok_result(
        data,
        source="synchronized_data",
        evidence_ids=[evidence_id] if evidence_id else [],
        freshness=freshness,
        status="ok" if runs else "partial",
        warnings=[] if runs else ["NO_RUNS_FOR_HEAD"],
    )
