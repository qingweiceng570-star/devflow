import copy
import importlib.util
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.integrations.github.client import GitHubClient
from app.integrations.github.read_adapter import GitHubRead
from app.persistence.models import RepoMembership, RunEvidence
from app.services.evidence_service import capture
from tests.test_platform_api import seed


def test_exact_sha_query_paginates_and_rejects_other_commits(settings):
    calls = []
    sha = "a" * 40

    def server(request):
        calls.append(request)
        if "page=2" in str(request.url):
            return httpx.Response(
                200, json={"workflow_runs": [{"id": 3, "head_sha": sha, "run_attempt": 2}]}
            )
        assert request.url.params["head_sha"] == sha
        return httpx.Response(
            200,
            json={"workflow_runs": [{"id": 1, "head_sha": "b" * 40}, {"id": 2, "head_sha": sha}]},
            headers={
                "Link": f'<https://api.github.com/repos/test/repo/actions/runs?head_sha={sha}&page=2>; rel="next"'
            },
        )

    read = GitHubRead(
        GitHubClient(settings, "", transport=httpx.MockTransport(server)), "test/repo"
    )
    limited = read.ci_for_head(sha, max_pages=1)
    assert limited["has_more"] and len(limited["items"]) == 1
    complete = read.ci_for_head(sha, max_pages=2)
    assert not complete["has_more"] and {r["id"] for r in complete["items"]} == {2, 3}


def test_signed_log_expiry_refreshes_once_without_credentials(settings):
    calls = []

    def server(request):
        calls.append(request)
        if request.url.host == "api.github.com":
            assert request.headers["authorization"] == "Bearer fake"
            return httpx.Response(
                302, headers={"Location": "https://logs.blob.core.windows.net/log?sig=temporary"}
            )
        assert "authorization" not in request.headers
        return httpx.Response(403 if len(calls) == 2 else 200, text="AssertionError: fixture")

    read = GitHubRead(
        GitHubClient(settings, "fake", transport=httpx.MockTransport(server)), "test/repo"
    )
    assert read.get_job_log(123, 1000) == (b"AssertionError: fixture", False)
    assert len(calls) == 4


def test_permission_denial_not_retried_or_mislabeled_expired(settings):
    calls = []

    def server(request):
        calls.append(request)
        return httpx.Response(403)

    read = GitHubRead(
        GitHubClient(settings, "fake", transport=httpx.MockTransport(server)), "test/repo"
    )
    with pytest.raises(Exception) as caught:
        read.get_job_log(123, 1000)
    assert caught.value.code == "GITHUB_PERMISSION_DENIED" and len(calls) == 1


def test_options_evidence_and_diagnostics_are_scoped(logged_in, database, settings):
    repo_id, run_id, _ = seed(database)
    with database() as db:
        evidence = capture(
            db,
            settings,
            repo_id=repo_id,
            source_type="code",
            content="fixture",
            locator={"path": "a.py"},
        )
        db.add_all(
            [
                RunEvidence(run_id=run_id, evidence_id=evidence.id, usage="collected"),
                RunEvidence(run_id=run_id, evidence_id=evidence.id, usage="cited"),
            ]
        )
        db.commit()
    assert logged_in.get(f"/api/v1/repos/{repo_id}/analysis-options").status_code == 200
    response = logged_in.get(f"/api/v1/runs/{run_id}/evidence")
    assert response.status_code == 200 and len(response.json()["data"]["items"]) == 1

    def server(request):
        return (
            httpx.Response(403) if "/actions/" in request.url.path else httpx.Response(200, json={})
        )

    logged_in.app.state.github_client_factory = lambda s, t: GitHubClient(
        s, t, transport=httpx.MockTransport(server)
    )
    result = logged_in.post(f"/api/v1/repos/{repo_id}/github-diagnostics", json={}).json()["data"]
    assert [r["status"] for r in result["checks"]] == ["available", "unavailable"]
    assert result["checks"][1]["code"] == "GITHUB_PERMISSION_DENIED"
    assert "未检查日志" in result["log_access"]
    with database() as db:
        membership = db.scalar(select(RepoMembership).where(RepoMembership.repo_id == repo_id))
        db.delete(membership)
        db.commit()
    assert logged_in.get(f"/api/v1/repos/{repo_id}/analysis-options").status_code == 404
    assert logged_in.get(f"/api/v1/runs/{run_id}/evidence").status_code == 404
    assert logged_in.post(f"/api/v1/repos/{repo_id}/github-diagnostics", json={}).status_code == 404


def test_real_case_scoring_rejects_stale_review_and_tampered_evidence():
    import json

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "real_evaluation", root / "scripts/evaluate_real.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    case = json.loads((root / "evals/real-v1/pr-16.json").read_text(encoding="utf-8"))
    review = json.loads((root / "evals/real-v1/reference-review.json").read_text(encoding="utf-8"))[
        "cases"
    ]["pr-16"]
    result = module.score(case, review)
    assert result["truth"] == {"supported": 0, "refuted": 2, "uncertain": 2, "pending": 0}
    assert result["citation_integrity"] == 1
    changed = copy.deepcopy(case)
    changed["output"]["summary"] = "new response"
    with pytest.raises(ValueError, match="different output"):
        module.score(changed, review)
    changed = copy.deepcopy(case)
    changed["evidence"][0]["content"] += "tampered"
    with pytest.raises(ValueError, match="hash mismatch"):
        module.score(changed, review)
