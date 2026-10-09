"""Recorded-shape fake GitHub API for replay tests (no network)."""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx

NOW = datetime.now(UTC)

HEAD_SHA = "1111111111111111111111111111111111111111"
OTHER_SHA = "2222222222222222222222222222222222222222"


def ts(days: float = 0) -> str:
    return (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def issue_payload(number: int, state: str, days_ago: float) -> dict:
    return {
        "id": 900000 + number,
        "number": number,
        "state": state,
        "title": f"issue {number}",
        "body": f"body of issue {number}",
        "user": {"login": "alice"},
        "labels": [{"name": "bug"}],
        "comments": 2,
        "html_url": f"https://github.com/octo/demo/issues/{number}",
        "created_at": ts(days_ago + 5),
        "updated_at": ts(days_ago),
    }


def pull_payload(number: int, state: str, days_ago: float, head_sha: str, merged: bool = False) -> dict:
    return {
        "id": 800000 + number,
        "number": number,
        "state": state,
        "draft": False,
        "title": f"pull {number}",
        "body": f"body of pull {number}",
        "user": {"login": "bob"},
        "labels": [],
        "base": {"ref": "main", "sha": OTHER_SHA},
        "head": {"ref": f"feature-{number}", "sha": head_sha},
        "html_url": f"https://github.com/octo/demo/pull/{number}",
        "created_at": ts(days_ago + 3),
        "updated_at": ts(days_ago),
        "merged_at": ts(days_ago) if merged else None,
    }


class FakeGitHub:
    """Routes the M1 sync surface; toggle flags simulate rate limiting and ETags."""

    def __init__(self, full_name: str = "octo/demo", external_id: int = 12345):
        self.full_name = full_name
        self.external_id = external_id
        self.requests: list[str] = []
        self.rate_limited_paths: set[str] = set()
        self.etag = 'W/"fake-v1"'
        self.etag_enabled = False
        self.fail_next: dict[str, int] = {}

    # -- payloads ---------------------------------------------------------
    def repo_payload(self) -> dict:
        return {
            "id": self.external_id,
            "full_name": self.full_name,
            "default_branch": "main",
        }

    def issues(self, state: str) -> list[dict]:
        if state == "open":
            entries = [issue_payload(1, "open", 1), issue_payload(2, "open", 2)]
            # The issues endpoint also returns PRs; sync must route them out.
            entries.append({**issue_payload(9, "open", 1), "pull_request": {"url": "x"}})
            return entries
        return [issue_payload(3, "closed", 1), issue_payload(4, "closed", 100)]

    def pulls(self, state: str) -> list[dict]:
        if state == "open":
            return [pull_payload(7, "open", 1, HEAD_SHA)]
        return [pull_payload(5, "closed", 2, OTHER_SHA, merged=True)]

    def reviews(self, number: int) -> list[dict]:
        return [
            {
                "id": 700000 + number,
                "state": "APPROVED",
                "commit_id": HEAD_SHA,
                "user": {"login": "carol"},
                "body": "looks good",
                "html_url": f"https://github.com/octo/demo/pull/{number}#r1",
                "submitted_at": ts(0.5),
            }
        ]

    def ci_runs(self) -> list[dict]:
        return [
            {
                "id": 600002,
                "run_attempt": 1,
                "name": "ci",
                "head_sha": HEAD_SHA,
                "head_branch": "feature-7",
                "event": "pull_request",
                "status": "completed",
                "conclusion": "failure",
                "html_url": "https://github.com/octo/demo/actions/runs/600002",
                "run_started_at": ts(0.8),
                "created_at": ts(0.9),
                "updated_at": ts(0.7),
            },
            {
                "id": 600001,
                "run_attempt": 1,
                "name": "ci",
                "head_sha": OTHER_SHA,
                "head_branch": "main",
                "event": "push",
                "status": "completed",
                "conclusion": "success",
                "html_url": "https://github.com/octo/demo/actions/runs/600001",
                "run_started_at": ts(1.5),
                "created_at": ts(1.6),
                "updated_at": ts(1.4),
            },
        ]

    def ci_jobs(self) -> list[dict]:
        return [
            {
                "id": 500001,
                "name": "test",
                "status": "completed",
                "conclusion": "failure",
                "html_url": "https://github.com/octo/demo/actions/runs/600002/job/500001",
                "started_at": ts(0.8),
                "completed_at": ts(0.7),
            }
        ]

    def events(self) -> list[dict]:
        return [
            {
                "id": "evt-1",
                "type": "IssuesEvent",
                "action": "opened",
                "actor": {"login": "alice"},
                "created_at": ts(1),
                "issue": {"number": 1},
            },
            {
                "id": "evt-2",
                "type": "PushEvent",
                "actor": {"login": "alice"},
                "created_at": ts(1),
            },
            {
                "id": "evt-3",
                "type": "PullRequestEvent",
                "action": "closed",
                "actor": {"login": "bob"},
                "created_at": ts(2),
                "pull_request": {"number": 5, "merged": True},
            },
        ]

    # -- transport --------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        url = urlsplit(str(request.url))
        path = url.path
        query = parse_qs(url.query)
        for prefix, status in self.fail_next.items():
            if path.startswith(prefix) and status:
                return httpx.Response(status, json={"message": "injected failure"})
        if path in self.rate_limited_paths:
            reset = int(NOW.timestamp()) + 30
            return httpx.Response(
                403,
                json={"message": "API rate limit exceeded"},
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)},
            )
        if self.etag_enabled and request.headers.get("if-none-match") == self.etag and path.startswith(
            (f"/repos/{self.full_name}/issues", f"/repos/{self.full_name}/pulls", "/repos")
        ):
            return httpx.Response(304)
        headers = {"etag": self.etag}

        if path == f"/repos/{self.full_name}":
            return httpx.Response(200, json=self.repo_payload())
        if path == f"/repos/{self.full_name}/issues":
            state = (query.get("state") or ["open"])[0]
            return httpx.Response(
                200, json=self.issues(state), headers=headers
            )
        if path == f"/repos/{self.full_name}/pulls":
            state = (query.get("state") or ["open"])[0]
            return httpx.Response(200, json=self.pulls(state), headers=headers)
        if path.startswith(f"/repos/{self.full_name}/pulls/") and path.endswith("/reviews"):
            number = int(path.split("/")[-2])
            return httpx.Response(200, json=self.reviews(number))
        if path == f"/repos/{self.full_name}/actions/runs":
            return httpx.Response(200, json={"workflow_runs": self.ci_runs()}, headers=headers)
        if "/actions/runs/" in path and path.endswith("/jobs"):
            return httpx.Response(200, json={"jobs": self.ci_jobs()})
        if path == f"/repos/{self.full_name}/events":
            return httpx.Response(200, json=self.events(), headers=headers)
        return httpx.Response(404, json={"message": "not found"})
