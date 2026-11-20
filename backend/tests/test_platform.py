import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.agents.budget import BudgetLedger, settle_interrupted
from app.core.errors import DomainError
from app.extensions.mcp import MCPClient, check_schema
from app.extensions.skills import Skill, execute_skill
from app.persistence.models import (
    ActionDraft,
    ActionExecution,
    ActivityEvent,
    BudgetAccount,
    BudgetReservation,
    Conversation,
    MemoryVersion,
    Repository,
    Run,
    RunEvidence,
    WeeklyReport,
)
from app.services import action_service as actions
from app.services import memory_service as memory
from app.services import run_service
from app.workflows.contracts import ReleaseTarget, WeeklyTarget
from app.workflows.release import synthesize
from tests.test_analyze import make_repo_and_conversation


def setup(database):
    repo_id, conversation_id = make_repo_and_conversation(database)
    with database() as db:
        actor = db.get(Conversation, conversation_id).owner_id
    return repo_id, conversation_id, actor


def completed(database):
    repo_id, conv_id, actor = setup(database)
    with database() as db:
        run = Run(repo_id=repo_id, conversation_id=conv_id, kind="pr_review", status="completed")
        db.add(run)
        db.flush()
        eid = uuid4()
        db.add(RunEvidence(run_id=run.id, evidence_id=eid, usage="collected"))
        db.commit()
        return repo_id, conv_id, actor, run.id, eid


@pytest.mark.parametrize(
    "tasks",
    [
        [{"key": "a", "agent": "knowledge_qa", "depends_on": ["b"]}],
        [{"key": "a", "agent": "knowledge_qa", "depends_on": ["a"]}],
        [{"key": "a", "agent": "knowledge_qa"}, {"key": "a", "agent": "knowledge_qa"}],
        [{"key": "a", "agent": "shell"}],
        [{"key": "observer_ci", "agent": "ci_debug"}],
    ],
)
def test_plan_rejects_cycles_duplicates_unknown_agents(tasks):
    with pytest.raises(ValidationError):
        ReleaseTarget(pr_number=1, tasks=tasks)


def test_release_request_selects_correct_target():
    from app.schemas.requests import SubmitRunRequest

    assert isinstance(
        SubmitRunRequest(kind="release_check", message="check", target={"pr_number": 1}).target,
        ReleaseTarget,
    )


@pytest.mark.parametrize(
    "fresh,policy,failed,expected",
    [
        (True, ["test"], False, "recommend_release"),
        (False, ["test"], False, "insufficient_evidence"),
        (True, [], False, "insufficient_evidence"),
        (True, ["test"], True, "insufficient_evidence"),
    ],
)
def test_release_never_promotes_incomplete_evidence(fresh, policy, failed, expected):
    tasks = [
        {
            "key": "pr",
            "required": True,
            "state": "completed",
            "result": {
                "kind": "pr_review",
                "coverage": "complete",
                "freshness": "fresh",
                "verdict": "recommend_merge",
            },
        },
        {
            "key": "ci",
            "required": True,
            "state": "failed" if failed else "completed",
            "result": {
                "kind": "ci_debug",
                "coverage": "complete",
                "freshness": "fresh",
                "details": {"conclusion": "success", "status": "completed"},
            },
        },
    ]
    result = synthesize(
        tasks, actor_id=uuid4(), repo_id=uuid4(), head_sha="a" * 40, head_fresh=fresh, policy=policy
    )
    assert result["verdict"] == expected
    tasks[1]["result"]["details"]["conclusion"] = "failure"
    assert (
        synthesize(
            tasks,
            actor_id=uuid4(),
            repo_id=uuid4(),
            head_sha="a" * 40,
            head_fresh=fresh,
            policy=policy,
        )["verdict"]
        == "hold"
    )


def test_synthesize_reports_observer_agents_and_warnings():
    tasks = [
        {
            "key": "pr",
            "agent": "pr_review",
            "required": True,
            "state": "completed",
            "result": {
                "kind": "pr_review",
                "summary": "PR 可以合入",
                "coverage": "complete",
                "freshness": "fresh",
                "verdict": "recommend_merge",
            },
        },
        {
            "key": "issue",
            "agent": "issue_triage",
            "required": True,
            "state": "completed",
            "result": {
                "kind": "issue_triage",
                "summary": "Issue 已分诊",
                "coverage": "partial",
                "freshness": "fresh",
                "verdict": "not_applicable",
                "details": {},
            },
        },
    ]
    result = synthesize(
        tasks,
        actor_id=uuid4(),
        repo_id=uuid4(),
        head_sha="a" * 40,
        head_fresh=True,
        policy=["test"],
        observation={"evidence_counts": {"pr": 49, "issue": 0}},
    )
    observer = result["details"]["observer"]
    assert [agent["key"] for agent in observer["agents"]] == ["pr", "issue"]
    assert observer["agents"][0]["evidence_count"] == 49
    assert observer["agents"][0]["confidence"] > observer["agents"][1]["confidence"]
    assert any("没有返回明确证据" in warning for warning in observer["warnings"])
    assert any("负责人建议为空" in warning for warning in observer["warnings"])


def test_cancel_wins_completion_and_no_assistant(database):
    repo_id, cid, _ = setup(database)
    with database() as db:
        run = Run(
            repo_id=repo_id,
            conversation_id=cid,
            kind="knowledge_qa",
            status="running",
            cancel_requested_at=datetime.now(UTC),
        )
        db.add(run)
        db.commit()
        assert not run_service.finalize_run(
            db, run, status="completed", assistant_text="must not appear"
        )
        assert run.status == "cancelled"
        from app.persistence.models import Message

        assert db.scalar(select(Message).where(Message.run_id == run.id)) is None


def test_shared_budget_atomic_parallel(pg_factory):
    with pg_factory() as db:
        repo = Repository(full_name="test/repo", external_id="1", credential_ref="github-default")
        db.add(repo)
        db.flush()
        parent = Run(repo_id=repo.id, kind="release_check")
        db.add(parent)
        db.flush()
        db.add(BudgetAccount(run_id=parent.id, limit_tokens=1000))
        children = [
            Run(repo_id=repo.id, parent_run_id=parent.id, kind="pr_review") for _ in range(3)
        ]
        db.add_all(children)
        db.commit()
        ids, parent_id = [r.id for r in children], parent.id

    def reserve(child_id):
        with pg_factory() as db:
            ledger = BudgetLedger(db, child_id, 1000)
            try:
                ledger.reserve(400)
                return True
            except DomainError:
                return False

    with ThreadPoolExecutor(max_workers=3) as pool:
        assert sum(pool.map(reserve, ids)) == 2
    with pg_factory() as db:
        settle_interrupted(db, parent_id)
        db.commit()
        row = db.scalar(select(BudgetAccount).where(BudgetAccount.run_id == parent_id))
        assert (row.reserved_tokens, row.consumed_tokens) == (0, 800)
        assert set(db.scalars(select(BudgetReservation.state))) == {"estimated"}


def test_memory_approval_scope_expiry_conflict_and_revocation(database):
    repo_id, _, actor, run_id, eid = completed(database)
    body = memory.MemoryInput(
        run_id=run_id,
        key="tests",
        content="Run tests before release",
        evidence_ids=[eid],
        commit_sha="a" * 40,
        paths=["src/*"],
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    with database() as db:
        candidate = memory.propose(db, repo_id, actor, body)
        assert memory.recall(db, repo_id, commit_sha="a" * 40, paths=["src/main.py"]) == []
        memory.decide(db, candidate, actor, "approved")
        assert len(memory.recall(db, repo_id, commit_sha="a" * 40, paths=["src/main.py"])) == 1
        assert memory.recall(db, repo_id, commit_sha="b" * 40, paths=["src/main.py"]) == []
        assert memory.recall(db, uuid4(), commit_sha="a" * 40, paths=["src/main.py"]) == []
        next_candidate = memory.propose(db, repo_id, actor, body)
        with pytest.raises(DomainError, match="需要明确"):
            memory.decide(db, next_candidate, actor, "approved")
        version = db.scalar(select(MemoryVersion))
        memory.decide(db, next_candidate, actor, "approved", [str(version.id)])
        assert version.state == "superseded"
        for row in db.scalars(select(MemoryVersion)):
            row.state = "revoked"
        db.commit()
        assert not memory.recall(db, repo_id, commit_sha="a" * 40, paths=["src/main.py"])


def test_memory_rejects_foreign_evidence(database):
    repo, _, actor, run, _ = completed(database)
    with database() as db, pytest.raises(DomainError):
        memory.propose(
            db,
            repo,
            actor,
            memory.MemoryInput(
                run_id=run,
                key="x",
                content="x",
                evidence_ids=[uuid4()],
                expires_at=datetime.now(UTC) + timedelta(days=1),
            ),
        )


class ActionRead:
    def __init__(self, changed=False, found=False):
        self.changed, self.found = changed, found
        self.marker = ""

    def get(self, path, params=None):
        if path == "/user":
            data = {"login": "testbot"}
        elif path.endswith("/comments"):
            data = (
                [
                    {
                        "id": 1,
                        "body": self.marker,
                        "user": {"login": "testbot"},
                        "created_at": datetime.now(UTC).isoformat(),
                    }
                ]
                if self.found
                else []
            )
        else:
            data = {
                "id": 1,
                "state": "open",
                "updated_at": "changed" if self.changed else "2026-10-01",
                "has_issues": True,
                "labels": [],
            }
        return SimpleNamespace(data=data, next_cursor=None)


def draft(database, settings):
    repo_id, _, actor = setup(database)
    with database() as db:
        repo = db.get(Repository, repo_id)
        row = actions.create_draft(
            db,
            settings,
            repo,
            actor,
            actions.ActionInput(action="create_issue_comment", number=1, body="Reviewed result"),
            client=ActionRead(),
        )
        return repo_id, actor, row.id


def test_approval_binds_body_revision_and_execution_unique(database, settings):
    repo_id, actor, draft_id = draft(database, settings)
    settings.enabled_actions = ["create_issue_comment"]
    settings.github_write_token = "write-secret"
    # SecretStr assignment isn't validated by Settings; use model_copy for real type.
    from pydantic import SecretStr

    settings.github_write_token = SecretStr("write-secret")
    with database() as db:
        row, repo = db.get(ActionDraft, draft_id), db.get(Repository, repo_id)
        settings.write_repo_allowlist = [repo.full_name]
        assert "devflow-action:" in row.payload["body"]
        with pytest.raises(DomainError):
            actions.approve(db, row, actor, 1, "0" * 64)
        actions.approve(db, row, actor, row.version, row.payload_hash)
        row = actions.create_draft(
            db,
            settings,
            repo,
            actor,
            actions.ActionInput(action="create_issue_comment", number=1, body="Changed"),
            client=ActionRead(),
            draft=row,
        )
        assert row.state == "pending" and row.approved_hash is None and row.version == 2
        with pytest.raises(DomainError):
            actions.execute(db, settings, repo, row, actor)
        actions.approve(db, row, actor, row.version, row.payload_hash)
        execution = actions.execute(db, settings, repo, row, actor)
        assert actions.execute(db, settings, repo, row, actor).id == execution.id
        actions.revoke(db, row, actor)
        assert row.state == "revoked" and execution.state == "cancelled"


@pytest.mark.parametrize(
    "status,expected",
    [(201, "succeeded"), (403, "failed"), (422, "failed"), (500, "unknown"), (302, "unknown")],
)
def test_writes_are_single_attempt(settings, status, expected):
    from pydantic import SecretStr

    settings.github_write_token = SecretStr("not-real")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"id": 4})

    result, _ = actions.send_once(
        settings,
        SimpleNamespace(full_name="test/repo"),
        SimpleNamespace(action="create_issue_comment", payload={"number": 1, "body": "x"}),
        transport=httpx.MockTransport(handler),
    )
    assert result == expected and len(calls) == 1


def test_unknown_reconcile_does_not_resend_or_assume_missing_failed(database, settings):
    repo_id, actor, draft_id = draft(database, settings)
    with database() as db:
        row, repo = db.get(ActionDraft, draft_id), db.get(Repository, repo_id)
        row.state = "unknown"
        execution = ActionExecution(
            draft_id=row.id,
            state="unknown",
            started_at=datetime.now(UTC),
            result={"expected_author": "testbot"},
        )
        db.add(execution)
        db.commit()
        client = ActionRead()
        actions.reconcile(db, settings, repo, row, actor, client=client)
        assert execution.state == "unknown"
        client.found, client.marker = True, row.payload["body"]
        actions.reconcile(db, settings, repo, row, actor, client=client)
        assert execution.state == "succeeded"


def test_weekly_half_open_window_and_repeat_stable(database, settings):
    from app.workflows.weekly import execute_weekly

    repo_id, cid, _ = setup(database)
    start = datetime(2026, 10, 4, 16, tzinfo=UTC)  # Monday midnight Shanghai
    end = start + timedelta(days=7)
    with database() as db:
        for index, instant in enumerate(
            [start - timedelta(seconds=1), start, end - timedelta(seconds=1), end]
        ):
            db.add(
                ActivityEvent(
                    repo_id=repo_id,
                    source_event_id=str(index),
                    kind="PushEvent",
                    occurred_at=instant,
                )
            )
        db.commit()
    for _ in range(2):
        with database() as db:
            run = run_service.submit_run(
                db,
                settings,
                conversation=db.get(Conversation, cid),
                repo=db.get(Repository, repo_id),
                kind="weekly_report",
                message_text="report",
                target={
                    "period_start": start.isoformat(),
                    "period_end": end.isoformat(),
                    "timezone": "Asia/Shanghai",
                },
            )
            run_id = run.id
        execute_weekly(database, settings, SimpleNamespace(entity_id=run_id), "test")
    with database() as db:
        rows = db.scalars(select(WeeklyReport)).all()
        assert len(rows) == 2
        assert all(r.content["details"]["counts"] == {"PushEvent": 2} for r in rows)


@pytest.mark.parametrize(
    "start,end,zone",
    [
        ("2026-01-01", "2026-01-02", "UTC"),
        ("2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z", "UTC"),
        ("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "invalid"),
    ],
)
def test_weekly_invalid_window(start, end, zone):
    with pytest.raises(ValidationError):
        WeeklyTarget(period_start=start, period_end=end, timezone=zone)


def test_mcp_enforces_allowlist_bound_and_session(settings):
    with pytest.raises(DomainError):
        MCPClient(settings, "http://127.0.0.1/mcp")
    settings.mcp_server_allowlist = ["https://mcp.example.test/mcp"]
    calls = []

    def server(request):
        import json

        body = json.loads(request.content)
        calls.append(body["method"])
        if body["method"] == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "session"},
                json={"id": body["id"], "result": {"protocolVersion": "2025-03-26"}},
            )
        assert request.headers["Mcp-Session-Id"] == "session"
        if "id" not in body:
            return httpx.Response(202)
        return httpx.Response(200, content=b"x" * 65537)

    client = MCPClient(
        settings, settings.mcp_server_allowlist[0], transport=httpx.MockTransport(server)
    )
    client.initialize()
    with pytest.raises(DomainError, match="64 KiB"):
        client.tools()
    client.close()
    assert calls == ["initialize", "notifications/initialized", "tools/list"]


def test_remote_schema_references_rejected():
    with pytest.raises(DomainError):
        check_schema({"$ref": "https://attacker.test/schema"})


def test_skill_no_code_or_cycles_and_cancel_stops_new_calls():
    for step in [
        {"id": "x", "kind": "shell"},
        {"id": "x", "kind": "condition"},
        {"id": "x", "kind": "tool", "tool": "delete"},
        {"id": "x", "kind": "transform", "source": "later.x"},
    ]:
        with pytest.raises(ValidationError):
            Skill(
                description="x", input_schema={}, output_schema={}, allowed_tools=[], steps=[step]
            )
    skill = Skill(
        description="x",
        input_schema={},
        output_schema={},
        allowed_tools=[],
        steps=[
            {"id": "a", "kind": "transform", "arguments": {"value": 1}},
            {"id": "b", "kind": "transform", "arguments": {"value": {"$from": "a.value"}}},
        ],
    )
    trace = []

    def stop():
        if trace:
            raise DomainError("CANCELLED", "cancelled", 409)

    with pytest.raises(DomainError):
        asyncio.run(execute_skill(skill, {}, None, stop, lambda *args: trace.append(args)))
    assert trace == [("a", "completed")]


@pytest.mark.parametrize(
    "status,conclusion",
    [
        ("in_progress", None),
        ("completed", None),
        ("completed", "cancelled"),
        ("completed", "skipped"),
    ],
)
def test_release_requires_completed_successful_ci(status, conclusion):
    tasks = [
        {
            "key": "pr",
            "required": True,
            "state": "completed",
            "result": {
                "kind": "pr_review",
                "coverage": "complete",
                "freshness": "fresh",
                "verdict": "recommend_merge",
            },
        },
        {
            "key": "ci",
            "required": True,
            "state": "completed",
            "result": {
                "kind": "ci_debug",
                "coverage": "complete",
                "freshness": "fresh",
                "details": {"status": status, "conclusion": conclusion},
            },
        },
    ]
    result = synthesize(
        tasks,
        actor_id=uuid4(),
        repo_id=uuid4(),
        head_sha="a" * 40,
        head_fresh=True,
        policy=["test"],
    )
    assert result["verdict"] == "insufficient_evidence"


def test_agent_draft_tool_is_local_scoped_and_idempotent(database, settings):
    from app.persistence.models import RepoMembership
    from app.tools.draft_tools import draft_spec

    repo_id, _, actor = setup(database)
    with database() as db:
        repo = db.get(Repository, repo_id)
        db.add(RepoMembership(repo_id=repo_id, user_id=actor, role="analyst"))
        db.commit()
    execution = SimpleNamespace(check_stop=lambda: None, draft_ids=set())
    deps = SimpleNamespace(
        db_factory=database,
        settings=settings,
        repo=repo,
        ctx=SimpleNamespace(actor_id=actor, run_id=uuid4(), pr_number=None, head_sha=None),
        github_read=SimpleNamespace(client=ActionRead()),
    )
    args = actions.ActionInput(action="create_issue_comment", number=1, body="Please review")
    spec = draft_spec(execution)
    first = asyncio.run(spec.handler(deps, args))
    second = asyncio.run(spec.handler(deps, args))
    assert first.data["draft_id"] == second.data["draft_id"]
    assert first.data["state"] == "pending"
    with database() as db:
        assert len(db.scalars(select(ActionDraft)).all()) == 1
        assert not db.scalars(select(ActionExecution)).all()
    deps.ctx.actor_id = uuid4()
    with pytest.raises(DomainError, match="无权"):
        asyncio.run(spec.handler(deps, args))


def test_mcp_fixed_version_scope_and_schema_change_pause(database, settings, monkeypatch):
    from app.extensions import registry
    from app.persistence.models import ExtensionConfig

    repo_id, _, _ = setup(database)
    tool = {
        "name": "lookup",
        "inputSchema": {
            "type": "object",
            "properties": {"repository": {"type": "string"}},
            "required": ["repository"],
            "additionalProperties": False,
        },
    }
    with database() as db:
        repo = db.get(Repository, repo_id)
        extension = ExtensionConfig(
            repo_id=repo_id,
            name="lookup",
            version=1,
            kind="mcp",
            schema_hash="fixed",
            content={
                "endpoint": "https://mcp.example.test/mcp",
                "allowed_tools": ["lookup"],
                "repo_argument": "repository",
                "manifest": {"lookup": tool},
            },
        )
        db.add(extension)
        db.commit()
        extension_id = extension.id
    calls, changed = [], []

    class Remote:
        def __init__(self, *_):
            pass

        def initialize(self):
            pass

        def close(self):
            pass

        def tools(self):
            return [{**tool, **({"description": "changed"} if changed else {})}]

        def rpc(self, method, params):
            calls.append(params)
            return {"content": [{"type": "text", "text": "result"}]}

    monkeypatch.setattr(registry, "MCPClient", Remote)
    execution = SimpleNamespace(
        db_factory=database,
        repo=repo,
        run=SimpleNamespace(config_snapshot={"extensions": [str(extension_id)]}),
        check_stop=lambda: None,
    )
    spec = registry.build_specs(execution, [])[0]
    deps = SimpleNamespace(
        db_factory=database, settings=settings, repo=repo, evidence=lambda **_: uuid4()
    )
    with pytest.raises(DomainError, match="仓库参数"):
        asyncio.run(
            spec.handler(deps, registry.ExtensionArguments(arguments={"repository": "other/repo"}))
        )
    assert not calls
    result = asyncio.run(spec.handler(deps, registry.ExtensionArguments()))
    assert result.evidence_ids and calls[0]["arguments"] == {"repository": repo.full_name}
    changed.append(True)
    with pytest.raises(DomainError, match="定义改变"):
        asyncio.run(spec.handler(deps, registry.ExtensionArguments()))
    assert len(calls) == 1
    with database() as db:
        assert db.get(ExtensionConfig, extension_id).state == "schema_changed"
    with pytest.raises(DomainError, match="停用"):
        asyncio.run(spec.handler(deps, registry.ExtensionArguments()))


def test_mcp_rejects_non_object_response(settings):
    settings.mcp_server_allowlist = ["https://mcp.example.test/mcp"]
    client = MCPClient(
        settings,
        settings.mcp_server_allowlist[0],
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[])),
    )
    with pytest.raises(DomainError, match="必须是对象"):
        client.rpc("tools/list")
    client.close()
