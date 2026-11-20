"""Isolated browser-test API. Synthetic data, mocked GitHub, no model/network calls.

Test control endpoints exist only in this harness, never in the application.
Every launch creates a fresh SQLite database under var/e2e/.
"""

import json
import re
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import httpx
import uvicorn
from sqlalchemy import select, text
from app.core.config import Settings
from app.main import create_app
from app.persistence.database import Base, create_database
from app.persistence.models import (
    CiRun,
    Conversation,
    Issue,
    Job,
    PullRequest,
    RepoMembership,
    Repository,
    Run,
    RunEvidence,
    TaskRun,
    User,
    WorkflowCheckpoint,
)
from app.integrations.github.client import GitHubClient
from app.jobs.queue import claim_next
from app.jobs.recovery import recover_expired
from app.services.auth_service import create_admin
from app.services import run_service
from app.services.evidence_service import capture
from app.workflows.release import execute_release

SHA = "a" * 40


def main():
    parent = ROOT / "var/e2e"
    parent.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="flows-", dir=parent))
    settings = Settings(
        _env_file=None,
        app_env="test",
        app_origin="http://localhost:13110",
        database_url=f"sqlite:///{(folder / 'test.db').as_posix()}",
        storage_root=folder,
        llm_provider="fixture",
    )
    engine, factory = create_database(settings)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
        )
        conn.execute(text("INSERT INTO alembic_version VALUES ('02d6b649101f')"))
    with factory() as db:
        create_admin(db, "flow-admin", "local-test-password-123")
        actor = db.scalar(select(User).where(User.username == "flow-admin"))
        repo = Repository(
            full_name="fixture/browser-flows", external_id="browser-flows"
        )
        db.add(repo)
        db.flush()
        repo_id, actor_id = repo.id, actor.id
        db.add(RepoMembership(repo_id=repo.id, user_id=actor.id, role="admin"))
        now = datetime.now(UTC)
        db.add(
            Issue(
                repo_id=repo.id,
                external_id="1",
                number=1,
                state="open",
                title="示例问题",
                source_updated_at=now,
            )
        )
        db.add(
            PullRequest(
                repo_id=repo.id,
                external_id="2",
                number=2,
                state="open",
                title="示例变更",
                head_sha=SHA,
                source_updated_at=now,
            )
        )
        conv = Conversation(repo_id=repo.id, owner_id=actor.id, title="已完成分析")
        db.add(conv)
        db.flush()
        empty_conversation_id = conv.id
        for attempt in (1, 2):
            db.add(
                CiRun(
                    repo_id=repo.id,
                    external_id="99",
                    attempt=attempt,
                    workflow_name="pytest",
                    head_sha=SHA,
                    status="completed",
                    conclusion="failure" if attempt == 2 else "success",
                    source_created_at=now,
                    source_updated_at=now,
                )
            )
        completed = Run(
            repo_id=repo.id,
            conversation_id=conv.id,
            kind="pr_review",
            status="completed",
            target={"pr_number": 2},
        )
        db.add(completed)
        db.flush()
        evidence = capture(
            db,
            settings,
            repo_id=repo.id,
            source_type="code",
            content="所有外部操作需要批准版本与哈希一致。",
            locator={"path": "src/policy.py"},
        )
        db.add(
            RunEvidence(run_id=completed.id, evidence_id=evidence.id, usage="collected")
        )
        db.commit()

    def github(request):
        path = request.url.path
        if re.fullmatch(r"/repos/[^/]+/[^/]+", path):
            # Repository import: echo the requested owner/repo back the way the
            # real API does so the browser flow can register it.
            return httpx.Response(
                200,
                json={
                    "id": 424242,
                    "full_name": path.removeprefix("/repos/"),
                    "default_branch": "main",
                },
            )
        if path.endswith("/actions/runs"):
            assert request.url.params.get("head_sha") in {None, SHA}
            return httpx.Response(
                200,
                json={
                    "workflow_runs": [
                        {
                            "id": 99,
                            "run_attempt": 2,
                            "name": "pytest",
                            "head_sha": SHA,
                            "status": "completed",
                            "conclusion": "success",
                            "created_at": "2026-10-01T00:00:00Z",
                        }
                    ]
                },
            )
        if "/pulls/" in path:
            return httpx.Response(200, json={"head": {"sha": SHA}})
        return httpx.Response(
            200,
            json={
                "id": 1,
                "state": "open",
                "updated_at": "2026-10-01T00:00:00Z",
                "has_issues": True,
            },
        )

    maker = lambda settings, token: GitHubClient(
        settings, token, transport=httpx.MockTransport(github)
    )
    app = create_app(settings, session_factory=factory)
    app.state.github_client_factory = maker

    @app.get("/test/state")
    def state():
        with factory() as db:
            return {
                "repo_id": str(repo_id),
                "empty_conversation_id": str(empty_conversation_id),
                "child_runs": len(
                    db.scalars(select(Run).where(Run.parent_run_id.is_not(None))).all()
                ),
            }

    @app.post("/test/seed-run")
    def seed_run(body: dict):
        with factory() as db:
            conv = Conversation(
                repo_id=repo_id,
                owner_id=actor_id,
                title="恢复验收" if body.get("workflow") else "取消验收",
            )
            db.add(conv)
            db.flush()
            run = run_service.submit_run(
                db,
                settings,
                conversation=conv,
                repo=db.get(Repository, repo_id),
                kind="release_check" if body.get("workflow") else "knowledge_qa",
                target={"pr_number": 2, "observer_rounds": 0}
                if body.get("workflow")
                else {},
                message_text="浏览器隔离样例",
            )
            if body.get("workflow"):
                job = claim_next(db, worker_id="interrupted", lease_seconds=60)
                run_service.mark_running(db, run)
                run.target = {**run.target, "head_sha": SHA}
                child = Run(
                    repo_id=repo_id,
                    parent_run_id=run.id,
                    kind="pr_review",
                    status="completed",
                    result={
                        "kind": "pr_review",
                        "summary": "已保存节点",
                        "verdict": "insufficient_evidence",
                        "coverage": "complete",
                        "freshness": "fresh",
                        "findings": [],
                        "hypotheses": [],
                        "limitations": [],
                    },
                )
                db.add(child)
                db.flush()
                db.add(
                    TaskRun(
                        run_id=run.id,
                        child_run_id=child.id,
                        key="review",
                        agent="pr_review",
                        state="completed",
                        specification={
                            "key": "review",
                            "agent": "pr_review",
                            "depends_on": [],
                            "required": True,
                            "target": {"pr_number": 2, "expected_head_sha": SHA},
                        },
                        result=child.result,
                    )
                )
                db.add(WorkflowCheckpoint(run_id=run.id, state={"node": "scheduler"}))
                job.lease_until = datetime.now(UTC) - timedelta(seconds=1)
                db.commit()
            return {
                "run_id": str(run.id),
                "conversation_id": str(conv.id),
                "repo_id": str(repo_id),
            }

    @app.post("/test/settle/{run_id}")
    def settle(run_id: UUID):
        with factory() as db:
            row = db.get(Run, run_id)
            assert row.repo_id == repo_id and row.cancel_requested_at
            run_service.finalize_run(db, row, status="cancelled")
            job = db.scalar(select(Job).where(Job.entity_id == run_id))
            job.status = "cancelled"
            db.commit()
        return {"status": "cancelled"}

    @app.post("/test/recover")
    def recover():
        return recover_expired(factory, settings)

    @app.post("/test/resume/{run_id}")
    def resume(run_id: UUID):
        with factory() as db:
            job = db.scalar(select(Job).where(Job.entity_id == run_id))
            job.available_at = datetime.now(UTC)
            db.commit()
            leased = claim_next(db, worker_id="resumed", lease_seconds=60)
            db.commit()
        return execute_release(
            factory, settings, leased, "resumed", client_factory=maker
        )

    uvicorn.run(app, host="127.0.0.1", port=18110, log_level="warning")


if __name__ == "__main__":
    main()
