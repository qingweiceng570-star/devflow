import httpx
import pytest
from sqlalchemy import func, select

from app.ingestion.github_sync import SyncRetry, run_sync
from app.integrations.github.client import GitHubClient
from app.jobs.queue import claim_next, enqueue
from app.persistence.models import (
    ActivityEvent,
    CiJob,
    CiRun,
    Issue,
    Job,
    PullRequest,
    Repository,
    Review,
    SourceSnapshot,
    SyncCursor,
    SyncRun,
)
from tests.fake_github import HEAD_SHA, FakeGitHub

ALL_RESOURCES = ["issues", "pulls", "reviews", "ci", "events"]


def client_factory_for(fake: FakeGitHub):
    def maker(settings, token):
        return GitHubClient(settings, token, transport=httpx.MockTransport(fake.handler))

    return maker


def setup_repo(database) -> str:
    with database() as db:
        repo = Repository(
            provider="github",
            external_id="12345",
            full_name="octo/demo",
            default_branch="main",
        )
        db.add(repo)
        db.commit()
        return repo.id


def make_run(database, repo_id) -> tuple:
    with database() as db:
        run = SyncRun(repo_id=repo_id, resources=ALL_RESOURCES, mode="incremental")
        db.add(run)
        db.flush()
        job = enqueue(db, kind="sync", entity_id=run.id, max_attempts=3)
        db.commit()
        return run.id, job.id


def claim(database, job_id):
    with database() as db:
        job = claim_next(db, worker_id="w1", lease_seconds=60)
        db.commit()
        assert job is not None and job.id == job_id
        return job.lease_epoch


def execute(database, settings, fake, run_id, job_id, epoch):
    return run_sync(
        database,
        settings,
        run_id,
        worker_id="w1",
        job_id=job_id,
        job_epoch=epoch,
        client_factory=client_factory_for(fake),
    )


def test_full_sync_then_idempotent_rerun(settings, database):
    settings.sync_max_pages = 4
    fake = FakeGitHub()
    repo_id = setup_repo(database)
    run_id, job_id = make_run(database, repo_id)
    epoch = claim(database, job_id)

    result = execute(database, settings, fake, run_id, job_id, epoch)
    assert result["status"] == "succeeded"
    counts = result["counts"]
    assert counts["issues"]["created"] == 3  # 2 open + 1 recently closed (PR item routed out)
    assert counts["pulls"]["created"] == 2
    assert counts["reviews"]["created"] == 1
    assert counts["ci"]["created"] == 3  # 2 runs + 1 job for the PR head run
    assert counts["events"]["created"] == 2 and counts["events"]["skipped"] == 1
    assert counts["coverage"] == "complete"

    with database() as db:
        assert db.scalar(select(func.count()).select_from(Issue)) == 3
        assert db.scalar(select(Issue.number).where(Issue.number == 9)) is None
        pr = db.scalar(select(PullRequest).where(PullRequest.number == 7))
        assert pr.head_sha == HEAD_SHA
        assert db.scalar(select(func.count()).select_from(Review)) == 1
        assert db.scalar(select(func.count()).select_from(CiRun)) == 2
        assert db.scalar(select(func.count()).select_from(CiJob)) == 1
        assert db.scalar(select(func.count()).select_from(ActivityEvent)) == 2
        run = db.get(SyncRun, run_id)
        assert run.status == "succeeded" and run.finished_at is not None
        cursor = db.scalar(
            select(SyncCursor).where(
                SyncCursor.repo_id == repo_id, SyncCursor.resource == "issues"
            )
        )
        assert cursor.committed_watermark is not None
        snapshots = db.scalar(select(func.count()).select_from(SourceSnapshot))
        assert snapshots == 8  # issues 3 + pulls 2 + review 1 + ci 2

    # Second run over the same content: nothing duplicated, no new snapshots.
    run2_id, job2_id = make_run(database, repo_id)
    epoch2 = claim(database, job2_id)
    result2 = execute(database, settings, fake, run2_id, job2_id, epoch2)
    assert result2["status"] == "succeeded"
    assert result2["counts"]["issues"]["created"] == 0
    assert result2["counts"]["pulls"]["created"] == 0
    with database() as db:
        assert db.scalar(select(func.count()).select_from(Issue)) == 3
        assert db.scalar(select(func.count()).select_from(PullRequest)) == 2
        assert db.scalar(select(func.count()).select_from(SourceSnapshot)) == 8
        # Closed issues were cut off by the watermark: nothing updated since.
        assert result2["counts"]["issues"]["fetched"] == 2


def test_page_cap_marks_partial_and_holds_watermark(settings, database):
    settings.sync_max_pages = 1
    fake = FakeGitHub()
    repo_id = setup_repo(database)
    run_id, job_id = make_run(database, repo_id)
    epoch = claim(database, job_id)

    result = execute(database, settings, fake, run_id, job_id, epoch)
    assert result["status"] == "succeeded"
    assert result["counts"]["coverage"] == "partial"
    assert result["counts"]["issues"]["capped"] is True
    with database() as db:
        cursor = db.scalar(
            select(SyncCursor).where(
                SyncCursor.repo_id == repo_id, SyncCursor.resource == "issues"
            )
        )
        assert cursor.committed_watermark is None


def test_rate_limit_requeues_run_and_retries_cleanly(settings, database):
    settings.sync_max_pages = 4
    settings.job_retry_max_delay_seconds = 60
    fake = FakeGitHub()
    fake.rate_limited_paths.add("/repos/octo/demo/pulls")
    repo_id = setup_repo(database)
    run_id, job_id = make_run(database, repo_id)
    epoch = claim(database, job_id)

    with pytest.raises(SyncRetry) as caught:
        execute(database, settings, fake, run_id, job_id, epoch)
    assert caught.value.retry_after is not None and caught.value.retry_after <= 31
    with database() as db:
        run = db.get(SyncRun, run_id)
        assert run.status == "queued"
        assert any(e["code"] == "SOURCE_RATE_LIMITED" for e in run.errors)
        assert run.counts["issues"]["created"] == 3  # earlier resource committed

    fake.rate_limited_paths.clear()
    result = execute(database, settings, fake, run_id, job_id, epoch)
    assert result["status"] == "succeeded"
    with database() as db:
        assert db.scalar(select(func.count()).select_from(Issue)) == 3
        assert db.scalar(select(func.count()).select_from(PullRequest)) == 2


def test_non_retryable_resource_error_records_and_continues(settings, database):
    settings.sync_max_pages = 4
    fake = FakeGitHub()
    fake.fail_next["/repos/octo/demo/pulls"] = 404
    repo_id = setup_repo(database)
    run_id, job_id = make_run(database, repo_id)
    epoch = claim(database, job_id)

    result = execute(database, settings, fake, run_id, job_id, epoch)
    assert result["status"] == "failed"
    codes = {error["code"] for error in result["errors"]}
    assert "SOURCE_UNAVAILABLE" in codes
    # Later resources still ran after the pulls failure.
    assert result["counts"]["events"]["created"] == 2
    assert result["counts"]["coverage"] == "partial"


def test_etag_not_modified_skips_rescan(settings, database):
    settings.sync_max_pages = 4
    fake = FakeGitHub()
    repo_id = setup_repo(database)
    run_id, job_id = make_run(database, repo_id)
    execute(database, settings, fake, run_id, job_id, claim(database, job_id))

    fake.etag_enabled = True
    run2_id, job2_id = make_run(database, repo_id)
    result = execute(database, settings, fake, run2_id, job2_id, claim(database, job2_id))
    assert result["counts"]["issues"]["pages"] == 2  # 304 for open + one closed call
    assert result["counts"]["issues"]["created"] == 0


def test_lease_lost_leaves_run_queued(settings, database):
    fake = FakeGitHub()
    repo_id = setup_repo(database)
    run_id, job_id = make_run(database, repo_id)
    epoch = claim(database, job_id)
    with database() as db:
        job = db.get(Job, job_id)
        job.status = "succeeded"
        job.lease_epoch += 1
        db.commit()

    result = execute(database, settings, fake, run_id, job_id, epoch)
    assert result == {"status": "queued", "note": "lease_lost"}
    with database() as db:
        assert db.get(SyncRun, run_id).status == "queued"
    assert fake.requests == []  # stopped before any upstream call
