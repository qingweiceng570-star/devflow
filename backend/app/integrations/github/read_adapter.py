"""Raw, paginated read operations against one repository. Normalization happens
in github_sync so snapshot payloads and internal rows stay in one place."""

from dataclasses import dataclass, field

from app.integrations.github.client import GitHubClient


@dataclass
class Page:
    items: list[dict] = field(default_factory=list)
    next_cursor: str | None = None
    etag: str | None = None
    not_modified: bool = False


def _iso(value) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


class GitHubRead:
    def __init__(self, client: GitHubClient, full_name: str):
        self.client = client
        self.full_name = full_name

    def get_repo(self) -> dict:
        return self.client.get(f"/repos/{self.full_name}").data

    def list_issues(
        self, state: str, since=None, cursor: str | None = None, etag: str | None = None
    ) -> Page:
        if cursor:
            page = self.client.get(cursor)
        else:
            params = {"state": state, "per_page": 100, "sort": "updated", "direction": "desc"}
            if since is not None:
                params["since"] = _iso(since)
            page = self.client.get(f"/repos/{self.full_name}/issues", params, etag=etag)
        if page.not_modified:
            return Page(not_modified=True)
        items = [item for item in page.data if "pull_request" not in item]
        return Page(items=items, next_cursor=page.next_cursor, etag=page.etag)

    def list_pulls(
        self, state: str, cursor: str | None = None, etag: str | None = None
    ) -> Page:
        if cursor:
            page = self.client.get(cursor)
        else:
            page = self.client.get(
                f"/repos/{self.full_name}/pulls",
                {"state": state, "per_page": 100, "sort": "updated", "direction": "desc"},
                etag=etag,
            )
        if page.not_modified:
            return Page(not_modified=True)
        return Page(items=list(page.data), next_cursor=page.next_cursor, etag=page.etag)

    def list_reviews(self, number: int, cursor: str | None = None) -> Page:
        if cursor:
            page = self.client.get(cursor)
        else:
            page = self.client.get(
                f"/repos/{self.full_name}/pulls/{number}/reviews", {"per_page": 100}
            )
        return Page(items=list(page.data), next_cursor=page.next_cursor)

    def list_ci_runs(self, created_since=None, cursor: str | None = None, etag: str | None = None) -> Page:
        if cursor:
            page = self.client.get(cursor)
        else:
            params = {"per_page": 100}
            if created_since is not None:
                params["created"] = f">={_iso(created_since)}"
            page = self.client.get(f"/repos/{self.full_name}/actions/runs", params, etag=etag)
        if page.not_modified:
            return Page(not_modified=True)
        return Page(items=list(page.data.get("workflow_runs") or []), next_cursor=page.next_cursor)

    def list_ci_jobs(self, run_external_id: str, attempt: int, cursor: str | None = None) -> Page:
        if cursor:
            page = self.client.get(cursor)
        else:
            page = self.client.get(
                f"/repos/{self.full_name}/actions/runs/{run_external_id}"
                f"/attempts/{attempt}/jobs",
                {"per_page": 100},
            )
        return Page(items=list(page.data.get("jobs") or []), next_cursor=page.next_cursor)

    def list_pr_files(self, number: int, cursor: str | None = None) -> Page:
        if cursor:
            page = self.client.get(cursor)
        else:
            page = self.client.get(
                f"/repos/{self.full_name}/pulls/{number}/files", {"per_page": 100}
            )
        return Page(items=list(page.data), next_cursor=page.next_cursor)

    def get_pr_live(self, number: int) -> dict:
        """Fresh PR metadata for fixing base/head at analysis start (README 3.2)."""
        return self.client.get(f"/repos/{self.full_name}/pulls/{number}").data

    def list_events(self, cursor: str | None = None, etag: str | None = None) -> Page:
        if cursor:
            page = self.client.get(cursor)
        else:
            page = self.client.get(
                f"/repos/{self.full_name}/events", {"per_page": 100}, etag=etag
            )
        if page.not_modified:
            return Page(not_modified=True)
        return Page(items=list(page.data), next_cursor=page.next_cursor)
