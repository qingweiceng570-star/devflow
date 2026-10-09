import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.domain.enums import JobStatus, RunStatus
from app.integrations.github.client import GitHubClient
from app.jobs.handlers.analyze import handle_analyze
from app.jobs.queue import claim_next
from app.jobs.recovery import recover_expired
from app.persistence.models import (
    Conversation,
    Job,
    Message,
    Repository,
    Run,
    RunEvent,
    RunEvidence,
    ToolCall,
    User,
)
from app.services import run_service
from app.workspace.manager import WorkspaceManager
from tests.fake_github import HEAD_SHA, FakeGitHub
from tests.scripted_model import ScriptedModel, pr_review_answer

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")


@pytest.fixture
def source_repo(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text(
        "\n".join(f"line {i}" for i in range(1, 30)) + "\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "x"], check=True)
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return repo, sha


def make_repo_and_conversation(database, *, full_name="octo/demo", config=None):
    with database() as db:
        user_id = db.scalar(select(User.id).where(User.username == "admin"))
        repo = Repository(
            provider="github",
            external_id="12345",
            full_name=full_name,
            default_branch="main",
            config_json=config or {},
        )
        db.add(repo)
        db.flush()
        conversation = Conversation(repo_id=repo.id, owner_id=user_id)
        db.add(conversation)
        db.commit()
        return repo.id, conversation.id


def submit(database, settings, conversation_id, pr_number=7):
    with database() as db:
        conversation = db.get(Conversation, conversation_id)
        repo = db.get(Repository, conversation.repo_id)
        run = run_service.submit_run(
            db,
            settings,
            conversation=conversation,
            repo=repo,
            message_text="请审查这个 PR 的风险",
            kind="pr_review",
            target={"pr_number": pr_number},
        )
        return run.id


def execute(database, settings, run_id, steps, fake, source_repo=None, on_request=None):
    model = ScriptedModel(steps, on_request=on_request)
    with database() as db:
        job = claim_next(db, worker_id="w1", lease_seconds=60)
        db.commit()
        assert job is not None and job.entity_id == run_id
        result = handle_analyze(
            database,
            settings,
            job,
            "w1",
            client_factory=lambda s, t: GitHubClient(s, t, transport=httpx.MockTransport(fake.handler)),
            model_factory=lambda s: model,
            workspace_factory=(
                (lambda s, t: WorkspaceManager(s, t, remote_url=lambda r: str(source_repo[0])))
                if source_repo
                else None
            ),
        )
    return result, model


def add_ci_run(database, repo_id, head_sha, workflow="ci", conclusion="success"):
    from app.persistence.models import CiRun

    with database() as db:
        db.add(
            CiRun(
                repo_id=repo_id,
                external_id=f"{head_sha[:8]}-{workflow}",
                attempt=1,
                workflow_name=workflow,
                head_sha=head_sha,
                status="completed",
                conclusion=conclusion,
                source_created_at=datetime.now(UTC) - timedelta(hours=1),
                source_updated_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        db.commit()


def test_complete_review_binds_diff_evidence(settings, database):
    fake = FakeGitHub()
    repo_id, conversation_id = make_repo_and_conversation(database)
    run_id = submit(database, settings, conversation_id)
    steps = [
        {"tool": "get_pr_diff", "args": {"pr_number": 7}},
        {"content_with_evidence": {"verdict": "hold", "severity": "high", "claim": "示例高风险变更"}},
    ]
    result, model = execute(database, settings, run_id, steps, fake)
    assert result["status"] == "completed"
    assert result["verdict"] == "hold"

    with database() as db:
        run = db.get(Run, run_id)
        assert run.status == RunStatus.COMPLETED and run.finished_at is not None
        assert run.result["verdict"] == "hold"
        assert len(run.result["findings"]) == 1
        assert run.result["findings"][0]["severity"] == "high"
        assert run.result["scope"]["repo_id"] == str(repo_id)
        # tool trace persisted with the prepared diff
        tools = db.scalars(select(ToolCall).order_by(ToolCall.created_at)).all()
        assert [t.tool for t in tools] == ["get_pr_diff"]
        assert tools[0].status == "ok"
        # evidence: prepared diff (collected) + cited binding
        bindings = db.scalars(select(RunEvidence)).all()
        usages = {b.usage for b in bindings}
        assert usages == {"collected", "cited"}
        # assistant message recorded
        assistant = db.scalar(
            select(Message).where(Message.run_id == run_id, Message.role == "assistant")
        )
        assert assistant is not None and "结论" in assistant.content
        # event sequence
        events = [
            row.type
            for row in db.scalars(select(RunEvent).order_by(RunEvent.seq))
        ]
        assert events[0] == "run.queued" and events[1] == "run.started"
        assert events[-1] == "run.completed"
        assert "tool.started" in events and "tool.finished" in events
        assert "evidence.added" in events


def test_recommend_merge_kept_only_when_everything_green(settings, database):
    fake = FakeGitHub()
    repo_id, conversation_id = make_repo_and_conversation(
        database, config={"required_checks": ["ci"]}
    )
    add_ci_run(database, repo_id, HEAD_SHA, conclusion="success")
    run_id = submit(database, settings, conversation_id)
    steps = [{"content": pr_review_answer("recommend_merge", summary="范围完整且检查通过")}]
    result, _ = execute(database, settings, run_id, steps, fake)
    assert result["verdict"] == "recommend_merge"


def test_recommend_merge_downgraded_when_checks_fail(settings, database):
    fake = FakeGitHub()
    repo_id, conversation_id = make_repo_and_conversation(
        database, config={"required_checks": ["ci"]}
    )
    add_ci_run(database, repo_id, HEAD_SHA, conclusion="failure")
    run_id = submit(database, settings, conversation_id)
    steps = [{"content": pr_review_answer("recommend_merge")}]
    result, _ = execute(database, settings, run_id, steps, fake)
    assert result["verdict"] == "hold"
    with database() as db:
        limitations = db.get(Run, run_id).result["limitations"]
        assert any("降级" in item for item in limitations)


def test_recommend_merge_downgraded_without_check_source(settings, database):
    fake = FakeGitHub()
    _, conversation_id = make_repo_and_conversation(database)
    run_id = submit(database, settings, conversation_id)
    steps = [{"content": pr_review_answer("recommend_merge")}]
    result, _ = execute(database, settings, run_id, steps, fake)
    assert result["verdict"] == "insufficient_evidence"


def test_fabricated_evidence_is_demoted_not_fatal(settings, database):
    fake = FakeGitHub()
    _, conversation_id = make_repo_and_conversation(database)
    run_id = submit(database, settings, conversation_id)
    fabricated = str(uuid4())
    steps = [{"content": pr_review_answer("hold", evidence_ids=[fabricated], severity="high")}]
    result, _ = execute(database, settings, run_id, steps, fake)
    assert result["status"] == "completed"
    with database() as db:
        run = db.get(Run, run_id)
        assert run.result["findings"] == []
        assert run.result["verdict"] == "hold"  # model's conservative call is kept
        assert any("移入局限性" in item for item in run.result["limitations"])


def test_head_changed_marks_stale_and_stops_positive(settings, database):
    fake = FakeGitHub()
    fake.mutate_after_pr_detail = 1  # a push lands between start and re-check
    _, conversation_id = make_repo_and_conversation(database)
    run_id = submit(database, settings, conversation_id)
    steps = [{"content": pr_review_answer("hold")}]
    result, _ = execute(database, settings, run_id, steps, fake)
    with database() as db:
        run = db.get(Run, run_id)
        assert run.result["freshness"] == "stale"
        assert run.stop_reason == "head_changed"


def test_cancel_between_steps(settings, database):
    fake = FakeGitHub()
    _, conversation_id = make_repo_and_conversation(database)
    run_id = submit(database, settings, conversation_id)

    def request_cancel_at_first_step(index: int) -> None:
        # Cancellation lands while the first model reply is in flight; the
        # loop must stop before acting on it.
        if index == 0:
            with database() as db:
                run = db.get(Run, run_id)
                run.cancel_requested_at = datetime.now(UTC)
                db.commit()

    steps = [
        {"tool": "get_repo_summary", "args": {}},
        {"content": pr_review_answer()},
    ]
    result, model = execute(
        database, settings, run_id, steps, fake, on_request=request_cancel_at_first_step
    )
    assert result["status"] == "cancelled"
    assert model.calls == 1  # the loop stopped after the in-flight reply
    with database() as db:
        run = db.get(Run, run_id)
        assert run.status == RunStatus.CANCELLED
        assert db.scalars(select(ToolCall)).all() == []  # no tool executed
        assert db.scalar(
            select(Message).where(Message.run_id == run_id, Message.role == "assistant")
        ) is None


def test_budget_exhausted_completes_with_partial_result(settings, database):
    settings.agent_total_token_budget = 1000
    fake = FakeGitHub()
    _, conversation_id = make_repo_and_conversation(database)
    run_id = submit(database, settings, conversation_id)
    steps = [{"content": pr_review_answer()}]
    result, model = execute(database, settings, run_id, steps, fake)
    assert result["status"] == "completed" and result["stop"] == "budget_exhausted"
    assert model.calls == 0  # stopped before any model call
    with database() as db:
        run = db.get(Run, run_id)
        assert run.status == RunStatus.COMPLETED
        assert run.stop_reason == "budget_exhausted"
        assert run.result["coverage"] == "partial"
        assert run.result["verdict"] == "insufficient_evidence"


def test_diff_cap_reports_coverage_and_unreviewed(settings, database):
    settings.max_diff_files = 1
    fake = FakeGitHub()
    _, conversation_id = make_repo_and_conversation(database)
    run_id = submit(database, settings, conversation_id)
    steps = [{"tool": "get_pr_diff", "args": {"pr_number": 7}}, {"content": pr_review_answer()}]
    result, _ = execute(database, settings, run_id, steps, fake)
    with database() as db:
        run = db.get(Run, run_id)
        assert run.result["coverage"] == "partial"
        assert any("未纳入审查" in item for item in run.result["limitations"])


def test_read_file_uses_pinned_sha_and_reads_code(settings, database, source_repo):
    repo_dir, sha = source_repo
    fake = FakeGitHub(head_sha=sha)
    _, conversation_id = make_repo_and_conversation(database)
    run_id = submit(database, settings, conversation_id)
    steps = [
        {"tool": "read_file", "args": {"relative_path": "src/app.py", "start_line": 1, "end_line": 3}},
        {"content_with_evidence": {"verdict": "hold", "severity": "medium"}},
    ]
    result, _ = execute(database, settings, run_id, steps, fake, source_repo=source_repo)
    assert result["status"] == "completed"
    with database() as db:
        tool = db.scalar(select(ToolCall).where(ToolCall.tool == "read_file"))
        assert tool.status == "ok"
        assert tool.result["data"]["commit_sha"] == sha
        assert tool.result["data"]["text"].startswith("line 1")
        assert str(tool.result["data"]["path"]) == "src/app.py"


def test_tool_budget_exceeded_returns_stable_error(settings, database, source_repo):
    settings.tool_result_max_bytes = 65536
    settings.tool_bytes_total_limit = 16384
    repo_dir, sha = source_repo
    big = "\n".join("x" * 120 for _ in range(300))  # ~36 KB single file
    (repo_dir / "big.txt").write_text(big, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-qm", "big"], check=True)
    new_sha = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    fake = FakeGitHub(head_sha=new_sha)
    _, conversation_id = make_repo_and_conversation(database)
    run_id = submit(database, settings, conversation_id)
    steps = [
        {"tool": "read_file", "args": {"relative_path": "big.txt"}},
        {"content": pr_review_answer()},
    ]
    result, _ = execute(database, settings, run_id, steps, fake, source_repo=source_repo)
    assert result["status"] == "completed"
    with database() as db:
        tool = db.scalar(select(ToolCall).where(ToolCall.tool == "read_file"))
        assert tool.status == "error" and tool.error_code == "TOOL_BUDGET_EXCEEDED"


def test_shared_evidence_cited_once_across_findings(settings, database):
    """Several findings may reuse the same evidence id without violating the
    (run_id, evidence_id, usage) uniqueness."""
    import json

    from app.integrations.llm.protocol import ModelReply

    fake = FakeGitHub()
    _, conversation_id = make_repo_and_conversation(database)
    run_id = submit(database, settings, conversation_id)

    class TwoFindingsModel(ScriptedModel):
        async def complete(self, request):
            self.requests.append(request)
            evidence_ids = self._latest_tool_evidence_ids(request)
            if not evidence_ids:
                return await super().complete(request)
            finding = {
                "severity": "medium",
                "claim": "同证据的发现",
                "evidence_ids": evidence_ids[:1],
                "location": "src/app.py:2",
                "suggestion": "请检查",
            }
            answer = pr_review_answer("hold")
            answer["findings"] = [finding, dict(finding)]
            return ModelReply(
                content=json.dumps(answer, ensure_ascii=False),
                finish_reason="stop",
                input_tokens=100,
                output_tokens=50,
            )

    with database() as db:
        job = claim_next(db, worker_id="w1", lease_seconds=60)
        db.commit()
        result = handle_analyze(
            database,
            settings,
            job,
            "w1",
            client_factory=lambda s, t: GitHubClient(s, t, transport=httpx.MockTransport(fake.handler)),
            model_factory=lambda s: TwoFindingsModel(
                [{"tool": "get_pr_diff", "args": {"pr_number": 7}}]
            ),
        )
    assert result["status"] == "completed"
    with database() as db:
        run = db.get(Run, run_id)
        assert len(run.result["findings"]) == 2
        cited = db.scalars(select(RunEvidence).where(RunEvidence.usage == "cited")).all()
        assert len(cited) == 1  # deduplicated binding


def test_recovery_fails_interrupted_analysis(settings, database):
    _, conversation_id = make_repo_and_conversation(database)
    run_id = submit(database, settings, conversation_id)
    with database() as db:
        job = claim_next(db, worker_id="w1", lease_seconds=60)
        # Simulate a worker that died after the run had started executing.
        run = db.get(Run, run_id)
        run.status = RunStatus.RUNNING
        run.started_at = datetime.now(UTC)
        job.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    recover_expired(database, settings)
    with database() as db:
        run = db.get(Run, run_id)
        assert run.status == RunStatus.FAILED
        assert run.error["code"] == "WORKER_INTERRUPTED"
        job = db.scalar(select(Job))
        assert job.status == JobStatus.FAILED
