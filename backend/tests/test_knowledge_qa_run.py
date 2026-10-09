import shutil
import subprocess

import httpx
import pytest
from sqlalchemy import select

from app.domain.enums import RunStatus
from app.integrations.github.client import GitHubClient
from app.jobs.handlers.analyze import handle_analyze
from app.jobs.queue import claim_next
from app.persistence.models import Conversation, Message, Repository, Run, ToolCall, User
from app.services import run_service
from app.workspace.manager import WorkspaceManager
from tests.fake_github import FakeGitHub
from tests.knowledge_helpers import make_repo, upload_and_index
from tests.scripted_model import ScriptedModel

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")

DOC = """# 认证设计

## 登录

设备码登录：用户在前端点击登录，终端显示设备码。

## 权限

maintainer 才能执行写操作，analyst 只能发起分析。
"""

NO_INDEX_DOC = DOC


def setup_conversation(database, settings, repo_id):
    with database() as db:
        user_id = db.scalar(select(User.id).where(User.username == "admin"))
        conversation = Conversation(repo_id=repo_id, owner_id=user_id)
        db.add(conversation)
        db.commit()
        return conversation.id


def submit(database, settings, conversation_id, message="项目里写操作的权限要求是什么？"):
    with database() as db:
        conversation = db.get(Conversation, conversation_id)
        repo = db.get(Repository, conversation.repo_id)
        return run_service.submit_run(
            db,
            settings,
            conversation=conversation,
            repo=repo,
            message_text=message,
            kind="knowledge_qa",
            target={},
        ).id


def execute(database, settings, run_id, steps):
    model = ScriptedModel(steps)
    fake = FakeGitHub()
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
        )
    return result, model


def test_knowledge_qa_with_citations(database, settings):
    repo_id = make_repo(database)
    upload_and_index(database, settings, repo_id, "auth.md", DOC)
    conversation_id = setup_conversation(database, settings, repo_id)
    run_id = submit(database, settings, conversation_id)

    steps = [
        {"tool": "search_knowledge", "args": {"query": "写操作 权限 maintainer"}},
        {
            "no_verdict": True,
            "content_with_evidence": {"severity": "info", "claim": "写操作需要 maintainer 角色"},
        },
    ]
    result, model = execute(database, settings, run_id, steps)
    assert result["status"] == "completed"

    with database() as db:
        run = db.get(Run, run_id)
        assert run.status == RunStatus.COMPLETED
        payload = run.result
        assert payload["kind"] == "knowledge_qa"
        assert payload["verdict"] == "not_applicable"
        assert payload["coverage"] == "complete"
        assert payload["freshness"] == "fresh"
        assert payload["findings"], payload
        finding = payload["findings"][0]
        assert "maintainer" in finding["claim"] or "maintainer" in finding["suggestion"]
        assert finding["evidence_ids"]
        tool = db.scalar(select(ToolCall).where(ToolCall.tool == "search_knowledge"))
        assert tool.status == "ok"
        chunk = tool.result["data"]["chunks"][0]
        assert chunk["document"] == "upload:auth.md"
        assert chunk["heading_path"] == ["认证设计", "权限"]
        assistant = db.scalar(
            select(Message).where(Message.run_id == run_id, Message.role == "assistant")
        )
        assert assistant is not None


def test_knowledge_qa_without_index_reports_gap(database, settings):
    repo_id = make_repo(database)
    conversation_id = setup_conversation(database, settings, repo_id)
    run_id = submit(database, settings, conversation_id)

    steps = [
        {"tool": "search_knowledge", "args": {"query": "权限"}},
        {"no_verdict": True, "content": {
            "summary": "知识库尚未建立索引，无法给出带引用的结论。",
            "findings": [],
            "hypotheses": [],
            "missing_evidence": [
                {"item": "项目文档", "reason": "知识库为空", "impact": "无法核对", "next_action": "上传文档"}
            ],
            "next_actions": [],
            "limitations": [],
        }},
    ]
    result, _ = execute(database, settings, run_id, steps)
    assert result["status"] == "completed"
    with database() as db:
        payload = db.get(Run, run_id).result
        assert payload["coverage"] == "unavailable"
        assert payload["freshness"] == "unknown"
        assert any("知识库尚未建立索引" in item for item in payload["limitations"])
        tool = db.scalar(select(ToolCall).where(ToolCall.tool == "search_knowledge"))
        assert tool.status == "partial"
        assert "no_index" in tool.result["data"]["degraded"]


def test_knowledge_qa_reads_code_at_branch_head(database, settings, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "t"], check=True)
    (source / "config.py").write_text("WRITE_ROLE = 'maintainer'\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "x"], check=True)
    sha = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    repo_id = make_repo(database)
    conversation_id = setup_conversation(database, settings, repo_id)
    submit(database, settings, conversation_id, message="写操作的角色常量是什么？")

    model = ScriptedModel([
        {"tool": "read_file", "args": {"relative_path": "config.py"}},
        {"no_verdict": True, "content_with_evidence": {"severity": "info", "claim": "WRITE_ROLE 常量"}},
    ])
    fake = FakeGitHub(head_sha=sha)
    with database() as db:
        job = claim_next(db, worker_id="w1", lease_seconds=60)
        db.commit()
        result = handle_analyze(
            database,
            settings,
            job,
            "w1",
            client_factory=lambda s, t: GitHubClient(s, t, transport=httpx.MockTransport(fake.handler)),
            model_factory=lambda s: model,
            workspace_factory=lambda s, t: WorkspaceManager(s, t, remote_url=lambda r: str(source)),
        )
    assert result["status"] == "completed"
    with database() as db:
        tool = db.scalar(select(ToolCall).where(ToolCall.tool == "read_file"))
        assert tool.status == "ok"
        assert tool.result["data"]["commit_sha"] == sha
        assert "WRITE_ROLE" in tool.result["data"]["text"]
