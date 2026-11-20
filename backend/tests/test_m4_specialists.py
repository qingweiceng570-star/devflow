import json

import httpx
import pytest
from pydantic import ValidationError

from app.core.errors import DomainError
from app.ingestion.ci_logs import parse_log
from app.integrations.github.client import GitHubClient
from app.persistence.models import Conversation, Repository, Run
from app.schemas.requests import SubmitRunRequest
from app.services import run_service
from tests.fake_github import HEAD_SHA, FakeGitHub, issue_payload
from tests.scripted_model import pr_review_answer
from tests.test_analyze import execute, make_repo_and_conversation


class SpecialistGitHub(FakeGitHub):
    def __init__(self, *, missing_log=False, wrong_attempt=False, changed=False):
        super().__init__()
        self.missing_log = missing_log
        self.wrong_attempt = wrong_attempt
        self.changed = changed
        self.issue_reads = 0

    def handler(self, request):
        path = request.url.path
        assert request.method == "GET"
        if "/branches/" in path:
            return httpx.Response(404)
        if path.endswith("/issues/1"):
            self.issue_reads += 1
            data = issue_payload(1, "open", 1)
            if self.changed and self.issue_reads > 1:
                data["updated_at"] = "2099-01-01T00:00:00Z"
            return httpx.Response(200, json=data)
        if path.endswith("/runs/99/attempts/2"):
            return httpx.Response(
                200,
                json={
                    "id": 99,
                    "run_attempt": 1 if self.wrong_attempt else 2,
                    "head_sha": HEAD_SHA,
                    "status": "completed",
                    "conclusion": "failure",
                    "updated_at": "2026-10-01T00:00:00Z",
                },
            )
        if path.endswith("/runs/99/attempts/2/jobs"):
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "id": 123,
                            "run_id": 99,
                            "run_attempt": 2,
                            "name": "pytest",
                            "status": "completed",
                            "conclusion": "failure",
                            "steps": [],
                        }
                    ]
                },
            )
        if path.endswith("/jobs/123/logs"):
            if self.missing_log:
                return httpx.Response(404)
            return httpx.Response(
                200,
                text="setup\npassword=hidden-value\nAssertionError: expected 2, got 1\nProcess completed with exit code 1\n",
            )
        return super().handler(request)


def submit_specialist(database, settings, conversation_id, kind, target):
    with database() as db:
        conversation = db.get(Conversation, conversation_id)
        repo = db.get(Repository, conversation.repo_id)
        return run_service.submit_run(
            db,
            settings,
            conversation=conversation,
            repo=repo,
            message_text="分析目标，给出证据与验证建议",
            kind=kind,
            target=target,
        ).id


@pytest.mark.parametrize(
    "kind,target",
    [
        ("ci_debug", {"ci_run_id": 99}),
        ("ci_debug", {"ci_run_id": 99, "attempt": 0}),
        ("issue_triage", {"pr_number": 1}),
        ("knowledge_qa", {"issue_number": 1}),
        ("pr_review", {"ci_run_id": 99, "attempt": 2}),
    ],
)
def test_target_schema_rejects_mismatched_kind(kind, target):
    with pytest.raises(ValidationError):
        SubmitRunRequest(message="test", kind=kind, target=target)


def test_issue_triage_drops_unverified_assignments_and_priority(database, settings):
    _, conversation = make_repo_and_conversation(database)
    run_id = submit_specialist(
        database, settings, conversation, "issue_triage", {"issue_number": 1}
    )
    answer = pr_review_answer()
    answer.pop("verdict")
    answer.update(
        category="bug",
        priority="P0",
        priority_reason="声称紧急",
        owner_candidates=[
            {
                "value": "invented",
                "reason": "猜测",
                "evidence_ids": ["00000000-0000-0000-0000-000000000001"],
            }
        ],
        missing_fields=["缺少复现版本"],
    )
    result, _ = execute(
        database,
        settings,
        run_id,
        [{"tool": "get_analysis_target"}, {"content": answer}],
        SpecialistGitHub(changed=True),
    )
    assert result["status"] == "completed"
    with database() as db:
        payload = db.get(Run, run_id).result
        assert payload["details"]["priority"] == "unknown"
        assert payload["details"]["owner_candidates"] == []
        assert payload["freshness"] == "stale"
        assert payload["verdict"] == "not_applicable"
        assert payload["details"]["missing_fields"] == ["缺少复现版本"]


@pytest.mark.parametrize("missing_log", [False, True])
def test_ci_diagnosis_retains_attempt_and_log_boundary(database, settings, missing_log):
    _, conversation = make_repo_and_conversation(database)
    run_id = submit_specialist(
        database, settings, conversation, "ci_debug", {"ci_run_id": 99, "attempt": 2}
    )
    answer = pr_review_answer()
    answer.pop("verdict")
    answer.update(cause_category="test", verification_steps=["检查断言前的实际输出"])
    result, model = execute(
        database,
        settings,
        run_id,
        [{"tool": "get_analysis_target"}, {"content": answer}],
        SpecialistGitHub(missing_log=missing_log),
    )
    assert result["status"] == "completed"
    with database() as db:
        payload = db.get(Run, run_id).result
        details = payload["details"]
        assert details["run"] == 99 and details["attempt"] == 2
        if missing_log:
            assert details["cause_category"] == "unknown"
            assert payload["coverage"] == "partial"
            assert details["log_diagnostics"][0]["code"] == "GITHUB_RESOURCE_UNAVAILABLE"
        else:
            assert details["error_blocks"][0]["line_start"] == 1
            assert "AssertionError" in details["error_blocks"][0]["text"]
            assert "hidden-value" not in json.dumps(payload)
            assert "hidden-value" not in json.dumps(model.requests[-1].messages)


@pytest.mark.parametrize("wrong_attempt,job_id", [(True, None), (False, 999)])
def test_ci_rejects_wrong_scope_before_model(database, settings, wrong_attempt, job_id):
    _, conversation = make_repo_and_conversation(database)
    target = {"ci_run_id": 99, "attempt": 2}
    if job_id:
        target["job_id"] = job_id
    run_id = submit_specialist(database, settings, conversation, "ci_debug", target)
    result, model = execute(
        database, settings, run_id, [], SpecialistGitHub(wrong_attempt=wrong_attempt)
    )
    assert result["status"] == "failed"
    assert model.calls == 0
    with database() as db:
        assert db.get(Run, run_id).error["code"] == "INVALID_TARGET"


def test_log_windows_preserve_original_lines_and_redact():
    lines = [f"line {i}" for i in range(100)]
    lines[42] = "\x1b[31mAssertionError: password=private-value api_key=secret-value\x1b[0m"
    lines[43] = "known-sensitive-token"
    result = parse_log("\n".join(lines).encode(), known_secrets=["known-sensitive-token"])
    block = result["blocks"][0]
    assert block["line_start"] == 13 and block["line_end"] == 73
    assert "private-value" not in block["text"] and "secret-value" not in block["text"]
    assert "known-sensitive-token" not in block["text"]
    assert "\x1b" not in block["text"]


def test_log_limits_and_archive_rejection():
    result = parse_log(("AssertionError " + "x" * 10000).encode(), truncated=True)
    assert result["truncated"] and result["warnings"]
    assert len(result["blocks"][0]["text"]) <= 2000
    assert not parse_log(b"PK\x03\x04fake archive")["blocks"]


def test_download_does_not_forward_token_and_limits_bytes(settings):
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(
                302, headers={"location": "https://logs.blob.core.windows.net/test?sig=private"}
            )
        return httpx.Response(200, content=b"a" * 3000)

    client = GitHubClient(settings, "private-token", transport=httpx.MockTransport(handler))
    data, truncated = client.download_log("/repos/octo/demo/actions/jobs/1/logs", 1024)
    assert len(data) == 1024 and truncated
    assert seen[0].headers["authorization"] == "Bearer private-token"
    assert "authorization" not in seen[1].headers


@pytest.mark.parametrize(
    "url",
    [
        "http://logs.blob.core.windows.net/x",
        "https://evil.example/x",
        "https://logs.blob.core.windows.net.evil.example/x",
        "https://127.0.0.1/x",
    ],
)
def test_download_rejects_untrusted_redirect(settings, url):
    client = GitHubClient(
        settings,
        "private-token",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(302, headers={"location": url})
        ),
    )
    with pytest.raises(DomainError, match="日志下载地址未获允许"):
        client.download_log("/repos/octo/demo/actions/jobs/1/logs", 1024)
