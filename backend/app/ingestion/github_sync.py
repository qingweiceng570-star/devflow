"""Bounded, resumable, idempotent GitHub synchronization (README 8).

One sync run walks the requested resources in a fixed order. Pages commit
individually so a crash or rate limit only loses the current page; upserts are
keyed on external IDs and snapshots are content-addressed, so re-running any
page is safe. Watermarks advance only after a resource's planned range
completed without hitting the page cap. A retry attempt re-describes the run
(counts and errors reset), relying on idempotent upserts.
"""

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.core.config import Settings
from app.core.errors import DomainError
from app.domain.enums import Coverage, SyncResource, SyncStatus
from app.integrations.github import normalizers
from app.integrations.github.client import GitHubClient, SourceRateLimited
from app.integrations.github.credentials import resolve_read_token
from app.integrations.github.read_adapter import GitHubRead, Page
from app.jobs import leases
from app.persistence.models import (
    ActivityEvent,
    CiJob,
    CiRun,
    Issue,
    PullRequest,
    Repository,
    Review,
    SourceSnapshot,
    SyncCursor,
    SyncRun,
    utcnow,
)
from app.storage.local import put_object

log = logging.getLogger("devflow.sync")


class SyncRetry(Exception):
    """The run was persisted back to queued; the job must retry later."""

    def __init__(self, retry_after: int | None = None):
        self.retry_after = retry_after


class LeaseLost(Exception):
    """The job lease moved to another owner; stop without touching job state."""


@dataclass
class ResourceResult:
    pages: int = 0
    fetched: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    snapshots: int = 0
    capped: bool = False

    def as_counts(self) -> dict:
        return {
            "pages": self.pages,
            "fetched": self.fetched,
            "created": self.created,
            "updated": self.updated,
            "skipped": self.skipped,
            "snapshots": self.snapshots,
            "capped": self.capped,
            "coverage": Coverage.PARTIAL if self.capped else Coverage.COMPLETE,
        }


@dataclass
class SyncContext:
    settings: Settings
    db: DBSession
    read: GitHubRead
    repo: Repository
    renew_job_lease: Callable[[], bool]
    run_started_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def renew_lease(self) -> None:
        if not self.renew_job_lease():
            raise LeaseLost()


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _upsert(db: DBSession, model, repo_id: UUID, external_id: str, fields: dict):
    row = db.scalar(
        select(model).where(model.repo_id == repo_id, model.external_id == external_id)
    )
    created = row is None
    if row is None:
        row = model(repo_id=repo_id, external_id=external_id)
        db.add(row)
    for key, value in fields.items():
        setattr(row, key, value)
    if hasattr(row, "updated_at"):
        row.updated_at = utcnow()
    return row, created


def _store_body(ctx: SyncContext, text: str | None, category: str) -> str | None:
    if not text:
        return None
    object_ref, _ = put_object(ctx.settings.storage_root, ctx.repo.id, category, text.encode())
    return object_ref


def _capture_snapshot(
    ctx: SyncContext, source_type: str, external_id: str, revision: str | None, raw: dict
) -> bool:
    """Store an immutable raw snapshot unless the identical revision is already kept."""
    content = json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()
    digest = hashlib.sha256(content).hexdigest()
    latest = ctx.db.scalar(
        select(SourceSnapshot)
        .where(
            SourceSnapshot.repo_id == ctx.repo.id,
            SourceSnapshot.source_type == source_type,
            SourceSnapshot.external_id == external_id,
        )
        .order_by(SourceSnapshot.created_at.desc())
        .limit(1)
    )
    if latest is not None and latest.content_hash == digest and latest.revision == revision:
        return False
    object_ref, digest = put_object(ctx.settings.storage_root, ctx.repo.id, "snapshots", content)
    ctx.db.add(
        SourceSnapshot(
            repo_id=ctx.repo.id,
            source_type=source_type,
            external_id=external_id,
            revision=revision,
            observed_at=utcnow(),
            object_ref=object_ref,
            content_hash=digest,
        )
    )
    return True


def _scan(
    ctx: SyncContext,
    res: ResourceResult,
    fetch_first: Callable[[str | None], Page],
    fetch_next: Callable[[str], Page],
    handle_item: Callable[[dict], None],
    stop_when_item: Callable[[dict], bool] | None = None,
    first_etag: str | None = None,
) -> tuple[str | None, bool]:
    """Drive one paginated scan over a resource.

    Returns (first_page_etag, stopped_early). Sets res.capped when the page
    budget runs out before the scan finished.
    """
    cursor = None
    is_first = True
    etag_out = first_etag
    while True:
        if res.pages >= ctx.settings.sync_max_pages:
            res.capped = True
            return etag_out, False
        ctx.renew_lease()
        page = fetch_first(first_etag) if is_first else fetch_next(cursor)
        res.pages += 1
        if is_first and page.etag:
            etag_out = page.etag
        is_first = False
        if page.not_modified:
            return etag_out, False
        for raw in page.items:
            if stop_when_item is not None and stop_when_item(raw):
                return etag_out, True
            handle_item(raw)
        if page.next_cursor is None:
            return etag_out, False
        cursor = page.next_cursor


def sync_issues(ctx: SyncContext, cursor_row: SyncCursor, res: ResourceResult) -> datetime | None:
    watermark = _aware(cursor_row.committed_watermark)
    overlap = timedelta(minutes=ctx.settings.sync_overlap_minutes)
    since = (watermark - overlap) if watermark else None
    cutoff = since or (datetime.now(UTC) - timedelta(days=ctx.settings.sync_issue_window_days))

    def handle(raw: dict) -> None:
        data = normalizers.normalize_issue(raw)
        res.fetched += 1
        _, created = _upsert(
            ctx.db,
            Issue,
            ctx.repo.id,
            data["external_id"],
            {
                "number": data["number"],
                "state": data["state"],
                "title": data["title"],
                "body_ref": _store_body(ctx, data["body"], "bodies"),
                "author_login": data["author_login"],
                "labels": data["labels"],
                "comments_count": data["comments_count"],
                "html_url": data["html_url"],
                "source_created_at": data["source_created_at"],
                "source_updated_at": data["source_updated_at"],
            },
        )
        res.created += created
        res.updated += not created
        revision = data["source_updated_at"].isoformat() if data["source_updated_at"] else None
        res.snapshots += _capture_snapshot(ctx, "issue", data["external_id"], revision, raw)

    etag, _ = _scan(
        ctx,
        res,
        lambda etag: ctx.read.list_issues("open", cursor=None, etag=etag),
        lambda cursor: ctx.read.list_issues("open", cursor=cursor),
        handle,
        first_etag=cursor_row.etag,
    )
    if etag:
        cursor_row.etag = etag
    if res.capped:
        return None

    def stop_closed(raw: dict) -> bool:
        updated = normalizers.parse_ts(raw.get("updated_at"))
        return bool(updated and updated < cutoff)

    _scan(
        ctx,
        res,
        lambda _etag: ctx.read.list_issues("closed", since=since, cursor=None),
        lambda cursor: ctx.read.list_issues("closed", cursor=cursor),
        handle,
        stop_when_item=stop_closed,
    )
    return None if res.capped else ctx.run_started_at


def sync_pulls(ctx: SyncContext, cursor_row: SyncCursor, res: ResourceResult) -> datetime | None:
    watermark = _aware(cursor_row.committed_watermark)
    overlap = timedelta(minutes=ctx.settings.sync_overlap_minutes)
    since = (watermark - overlap) if watermark else None
    cutoff = since or (datetime.now(UTC) - timedelta(days=ctx.settings.sync_issue_window_days))

    def handle(raw: dict) -> None:
        data = normalizers.normalize_pull(raw)
        res.fetched += 1
        _, created = _upsert(
            ctx.db,
            PullRequest,
            ctx.repo.id,
            data["external_id"],
            {
                "number": data["number"],
                "state": data["state"],
                "merged": data["merged"],
                "draft": data["draft"],
                "title": data["title"],
                "body_ref": _store_body(ctx, data["body"], "bodies"),
                "author_login": data["author_login"],
                "labels": data["labels"],
                "base_ref": data["base_ref"],
                "base_sha": data["base_sha"],
                "head_ref": data["head_ref"],
                "head_sha": data["head_sha"],
                "html_url": data["html_url"],
                "source_created_at": data["source_created_at"],
                "source_updated_at": data["source_updated_at"],
                "merged_at": data["merged_at"],
            },
        )
        res.created += created
        res.updated += not created
        revision = data["source_updated_at"].isoformat() if data["source_updated_at"] else None
        res.snapshots += _capture_snapshot(ctx, "pull_request", data["external_id"], revision, raw)

    etag, _ = _scan(
        ctx,
        res,
        lambda etag: ctx.read.list_pulls("open", cursor=None, etag=etag),
        lambda cursor: ctx.read.list_pulls("open", cursor=cursor),
        handle,
        first_etag=cursor_row.etag,
    )
    if etag:
        cursor_row.etag = etag
    if res.capped:
        return None

    def stop_closed(raw: dict) -> bool:
        updated = normalizers.parse_ts(raw.get("updated_at"))
        return bool(updated and updated < cutoff)

    _scan(
        ctx,
        res,
        lambda _etag: ctx.read.list_pulls("closed", cursor=None),
        lambda cursor: ctx.read.list_pulls("closed", cursor=cursor),
        handle,
        stop_when_item=stop_closed,
    )
    return None if res.capped else ctx.run_started_at


def sync_reviews(ctx: SyncContext, cursor_row: SyncCursor, res: ResourceResult) -> datetime | None:
    watermark = _aware(cursor_row.committed_watermark)
    overlap = timedelta(minutes=ctx.settings.sync_overlap_minutes)
    since = (watermark - overlap) if watermark else None
    conditions = [PullRequest.state == "open"]
    if since is not None:
        conditions.append(PullRequest.source_updated_at >= since)
    prs = ctx.db.scalars(
        select(PullRequest)
        .where(PullRequest.repo_id == ctx.repo.id, *conditions)
        .order_by(PullRequest.source_updated_at.desc())
        .limit(ctx.settings.sync_review_max_prs)
    ).all()
    for pr in prs:
        if res.pages >= ctx.settings.sync_max_pages:
            res.capped = True
            break
        ctx.renew_lease()
        page = ctx.read.list_reviews(pr.number)
        res.pages += 1
        for raw in page.items:
            data = normalizers.normalize_review(raw)
            res.fetched += 1
            _, created = _upsert(
                ctx.db,
                Review,
                ctx.repo.id,
                data["external_id"],
                {
                    "pr_id": pr.id,
                    "state": data["state"],
                    "commit_sha": data["commit_sha"],
                    "author_login": data["author_login"],
                    "body_ref": _store_body(ctx, data["body"], "bodies"),
                    "html_url": data["html_url"],
                    "submitted_at": data["submitted_at"],
                },
            )
            res.created += created
            res.updated += not created
            revision = data["submitted_at"].isoformat() if data["submitted_at"] else None
            res.snapshots += _capture_snapshot(ctx, "review", data["external_id"], revision, raw)
    return None  # reviews carry no watermark


def _upsert_ci_run(db: DBSession, repo_id: UUID, data: dict):
    row = db.scalar(
        select(CiRun).where(
            CiRun.repo_id == repo_id,
            CiRun.external_id == data["external_id"],
            CiRun.attempt == data["attempt"],
        )
    )
    created = row is None
    if row is None:
        row = CiRun(repo_id=repo_id, external_id=data["external_id"], attempt=data["attempt"])
        db.add(row)
    for key in (
        "workflow_name",
        "head_sha",
        "head_branch",
        "event",
        "status",
        "conclusion",
        "html_url",
        "run_started_at",
        "source_created_at",
        "source_updated_at",
    ):
        setattr(row, key, data[key])
    row.updated_at = utcnow()
    return row, created


def sync_ci(ctx: SyncContext, cursor_row: SyncCursor, res: ResourceResult) -> datetime | None:
    watermark = _aware(cursor_row.committed_watermark)
    overlap = timedelta(minutes=ctx.settings.sync_overlap_minutes)
    since = (watermark - overlap) if watermark else None
    created_since = since or (datetime.now(UTC) - timedelta(days=ctx.settings.sync_ci_window_days))

    def handle(raw: dict) -> None:
        data = normalizers.normalize_ci_run(raw)
        res.fetched += 1
        _, created = _upsert_ci_run(ctx.db, ctx.repo.id, data)
        res.created += created
        res.updated += not created
        revision = f"{data['attempt']}:{data['source_updated_at'].isoformat() if data['source_updated_at'] else ''}"
        res.snapshots += _capture_snapshot(ctx, "ci_run", data["external_id"], revision, raw)

    etag, _ = _scan(
        ctx,
        res,
        lambda etag: ctx.read.list_ci_runs(created_since=created_since, etag=etag),
        lambda cursor: ctx.read.list_ci_runs(cursor=cursor),
        handle,
        first_etag=cursor_row.etag,
    )
    if etag:
        cursor_row.etag = etag
    complete = not res.capped

    if ctx.settings.sync_ci_job_max_runs:
        head_shas = ctx.db.scalars(
            select(PullRequest.head_sha)
            .where(PullRequest.repo_id == ctx.repo.id, PullRequest.head_sha.is_not(None))
            .distinct()
        ).all()
        if head_shas:
            runs = ctx.db.scalars(
                select(CiRun)
                .where(CiRun.repo_id == ctx.repo.id, CiRun.head_sha.in_(head_shas))
                .order_by(CiRun.source_created_at.desc())
                .limit(ctx.settings.sync_ci_job_max_runs)
            ).all()
            for run in runs:
                ctx.renew_lease()
                page = ctx.read.list_ci_jobs(run.external_id, run.attempt)
                res.pages += 1
                for raw in page.items:
                    data = normalizers.normalize_ci_job(raw)
                    res.fetched += 1
                    _, created = _upsert(
                        ctx.db,
                        CiJob,
                        ctx.repo.id,
                        data["external_id"],
                        {
                            "ci_run_id": run.id,
                            "name": data["name"],
                            "status": data["status"],
                            "conclusion": data["conclusion"],
                            "html_url": data["html_url"],
                            "started_at": data["started_at"],
                            "completed_at": data["completed_at"],
                        },
                    )
                    res.created += created
                    res.updated += not created
    return ctx.run_started_at if complete else None


def sync_events(ctx: SyncContext, cursor_row: SyncCursor, res: ResourceResult) -> datetime | None:
    def handle(raw: dict) -> None:
        data = normalizers.normalize_event(raw)
        if data is None:
            res.skipped += 1
            return
        res.fetched += 1
        existing = ctx.db.scalar(
            select(ActivityEvent).where(
                ActivityEvent.repo_id == ctx.repo.id,
                ActivityEvent.source_event_id == data["source_event_id"],
            )
        )
        if existing is not None:
            res.skipped += 1
            return
        object_ref, _ = put_object(
            ctx.settings.storage_root,
            ctx.repo.id,
            "snapshots",
            json.dumps(raw, ensure_ascii=False, sort_keys=True).encode(),
        )
        ctx.db.add(
            ActivityEvent(
                repo_id=ctx.repo.id,
                source_event_id=data["source_event_id"],
                kind=data["kind"],
                actor_login=data["actor_login"],
                subject_type=data["subject_type"],
                subject_number=data["subject_number"],
                occurred_at=data["occurred_at"],
                payload_ref=object_ref,
            )
        )
        res.created += 1
        res.snapshots += 1

    etag, _ = _scan(
        ctx,
        res,
        lambda etag: ctx.read.list_events(cursor=None, etag=etag),
        lambda cursor: ctx.read.list_events(cursor=cursor),
        handle,
        first_etag=cursor_row.etag,
    )
    if etag:
        cursor_row.etag = etag
    return ctx.run_started_at if not res.capped else None


HANDLERS: dict[SyncResource, Callable] = {
    SyncResource.ISSUES: sync_issues,
    SyncResource.PULLS: sync_pulls,
    SyncResource.REVIEWS: sync_reviews,
    SyncResource.CI: sync_ci,
    SyncResource.EVENTS: sync_events,
}

RESOURCE_ORDER = (
    SyncResource.ISSUES,
    SyncResource.PULLS,
    SyncResource.REVIEWS,
    SyncResource.CI,
    SyncResource.EVENTS,
)


def create_read_client(settings: Settings, token: str) -> GitHubClient:
    return GitHubClient(settings, token)


def _get_or_create_cursor(db: DBSession, repo_id: UUID, resource: str) -> SyncCursor:
    row = db.scalar(
        select(SyncCursor).where(SyncCursor.repo_id == repo_id, SyncCursor.resource == resource)
    )
    if row is None:
        row = SyncCursor(repo_id=repo_id, resource=resource)
        db.add(row)
        db.flush()
    return row


def run_sync(
    session_factory,
    settings: Settings,
    sync_run_id: UUID,
    *,
    worker_id: str,
    job_id: UUID,
    job_epoch: int,
    client_factory=None,
) -> dict:
    """Execute one sync run. Raises SyncRetry when the job should retry later."""
    from app.jobs.queue import renew

    maker = client_factory or create_read_client

    def renew_job_lease() -> bool:
        return renew(
            session_factory,
            job_id=job_id,
            worker_id=worker_id,
            epoch=job_epoch,
            lease_seconds=settings.job_lease_seconds,
        )

    with session_factory() as db:
        run = db.get(SyncRun, sync_run_id)
        if run is None:
            raise DomainError("SYNC_RUN_MISSING", "同步记录不存在", 404)
        repo = db.get(Repository, run.repo_id)
        if repo is None:
            raise DomainError("REPO_MISSING", "仓库记录不存在", 404)
        now = datetime.now(UTC)
        if run.started_at is None:
            run.started_at = now
        run.status = SyncStatus.RUNNING
        run.counts = {}
        run.errors = []
        run.finished_at = None
        run.updated_at = now
        db.commit()
        run_started_at = _aware(run.started_at)

        token = resolve_read_token(settings, repo.credential_ref)
        read = GitHubRead(maker(settings, token), repo.full_name)
        ctx = SyncContext(
            settings=settings,
            db=db,
            read=read,
            repo=repo,
            renew_job_lease=renew_job_lease,
            run_started_at=run_started_at,
        )
        counts: dict = {}
        errors: list = []
        lease_owner = f"{worker_id}:{run.id}"
        halted: DomainError | None = None
        resources = [r for r in RESOURCE_ORDER if r in (run.resources or [])]

        for resource in resources:
            cursor_row = _get_or_create_cursor(db, repo.id, str(resource))
            res = ResourceResult()
            acquired = leases.acquire(
                db,
                repo_id=repo.id,
                resource_kind="sync",
                resource_key=str(resource),
                owner=lease_owner,
                ttl_seconds=settings.job_lease_seconds * 5,
            )
            db.commit()
            if not acquired:
                counts[resource] = {"skipped": "resource_busy"}
                continue
            try:
                try:
                    new_watermark = HANDLERS[resource](ctx, cursor_row, res)
                except LeaseLost:
                    raise
                counts[resource] = res.as_counts()
                if new_watermark is not None:
                    cursor_row.committed_watermark = new_watermark
                cursor_row.coverage = {
                    "last_coverage": counts[resource]["coverage"],
                    "at": datetime.now(UTC).isoformat(),
                }
                cursor_row.updated_at = datetime.now(UTC)
                db.commit()
            except SourceRateLimited as limited:
                db.rollback()
                counts[resource] = res.as_counts()
                errors.append(
                    {
                        "resource": str(resource),
                        "code": "SOURCE_RATE_LIMITED",
                        "message": "GitHub 额度限制，等待后续重试",
                    }
                )
                halted = limited
            except DomainError as error:
                db.rollback()
                counts[resource] = {
                    "pages": res.pages,
                    "fetched": res.fetched,
                    "error": error.code,
                    "coverage": Coverage.PARTIAL,
                }
                errors.append(
                    {"resource": str(resource), "code": error.code, "message": error.message}
                )
                if error.retryable:
                    halted = error
            except LeaseLost:
                db.rollback()
                counts[resource] = res.as_counts()
                run.status = SyncStatus.QUEUED
                run.counts = counts
                run.errors = errors
                run.updated_at = datetime.now(UTC)
                db.commit()
                return {"status": str(SyncStatus.QUEUED), "note": "lease_lost"}
            finally:
                leases.release(
                    db,
                    repo_id=repo.id,
                    resource_kind="sync",
                    resource_key=str(resource),
                    owner=lease_owner,
                )
                db.commit()
            if halted is not None:
                break

        coverage = Coverage.COMPLETE
        for entry in counts.values():
            if not isinstance(entry, dict):
                continue
            # "skipped" as a marker string means the resource was not processed;
            # an integer skip count (unmapped upstream items) is normal.
            if entry.get("capped") or entry.get("error") or entry.get("skipped") == "resource_busy":
                coverage = Coverage.PARTIAL
                break
        counts["coverage"] = coverage
        run.counts = counts
        run.errors = errors
        run.updated_at = datetime.now(UTC)
        if halted is not None:
            run.status = SyncStatus.QUEUED
            db.commit()
            raise SyncRetry(getattr(halted, "retry_after", None))
        run.status = SyncStatus.FAILED if errors else SyncStatus.SUCCEEDED
        run.finished_at = datetime.now(UTC)
        db.commit()
        return {"status": str(run.status), "counts": counts, "errors": errors}
