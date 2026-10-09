"""Normalize raw GitHub API payloads into internal row dictionaries."""

from datetime import UTC, datetime

KNOWN_CI_CONCLUSIONS = {
    "success",
    "failure",
    "cancelled",
    "timed_out",
    "skipped",
    "action_required",
    "neutral",
    "stale",
    "startup_failure",
}


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _labels(payload: dict) -> list[str]:
    return [item["name"] for item in payload.get("labels") or [] if isinstance(item, dict)]


def _author(payload: dict) -> str | None:
    user = payload.get("user") or {}
    return user.get("login")


def normalize_repo(payload: dict) -> dict:
    return {
        "external_id": str(payload["id"]),
        "full_name": payload["full_name"],
        "default_branch": payload.get("default_branch"),
    }


def normalize_issue(payload: dict) -> dict:
    return {
        "external_id": str(payload["id"]),
        "number": int(payload["number"]),
        "state": payload["state"],
        "title": payload.get("title") or "",
        "body": payload.get("body"),
        "author_login": _author(payload),
        "labels": _labels(payload),
        "comments_count": int(payload.get("comments") or 0),
        "html_url": payload.get("html_url"),
        "source_created_at": parse_ts(payload.get("created_at")),
        "source_updated_at": parse_ts(payload.get("updated_at")),
    }


def normalize_pull(payload: dict) -> dict:
    base = payload.get("base") or {}
    head = payload.get("head") or {}
    merged_at = parse_ts(payload.get("merged_at"))
    return {
        "external_id": str(payload["id"]),
        "number": int(payload["number"]),
        "state": payload["state"],
        "merged": bool(merged_at),
        "draft": bool(payload.get("draft")),
        "title": payload.get("title") or "",
        "body": payload.get("body"),
        "author_login": _author(payload),
        "labels": _labels(payload),
        "base_ref": base.get("ref"),
        "base_sha": base.get("sha"),
        "head_ref": head.get("ref"),
        "head_sha": head.get("sha"),
        "html_url": payload.get("html_url"),
        "source_created_at": parse_ts(payload.get("created_at")),
        "source_updated_at": parse_ts(payload.get("updated_at")),
        "merged_at": merged_at,
    }


def normalize_review(payload: dict) -> dict:
    return {
        "external_id": str(payload["id"]),
        "state": (payload.get("state") or "unknown").lower(),
        "commit_sha": payload.get("commit_id"),
        "author_login": _author(payload),
        "body": payload.get("body"),
        "html_url": payload.get("html_url"),
        "submitted_at": parse_ts(payload.get("submitted_at")),
    }


def normalize_ci_run(payload: dict) -> dict:
    return {
        "external_id": str(payload["id"]),
        "attempt": int(payload.get("run_attempt") or 1),
        "workflow_name": payload.get("name") or "",
        "head_sha": payload.get("head_sha") or "",
        "head_branch": payload.get("head_branch"),
        "event": payload.get("event"),
        "status": payload.get("status") or "unknown",
        "conclusion": payload.get("conclusion"),
        "html_url": payload.get("html_url"),
        "run_started_at": parse_ts(payload.get("run_started_at")),
        "source_created_at": parse_ts(payload.get("created_at")),
        "source_updated_at": parse_ts(payload.get("updated_at")),
    }


def normalize_ci_job(payload: dict) -> dict:
    return {
        "external_id": str(payload["id"]),
        "name": payload.get("name") or "",
        "status": payload.get("status") or "unknown",
        "conclusion": payload.get("conclusion"),
        "html_url": payload.get("html_url"),
        "started_at": parse_ts(payload.get("started_at")),
        "completed_at": parse_ts(payload.get("completed_at")),
    }


def normalize_event(payload: dict) -> dict | None:
    """Map a repository activity event to our event kinds; unknown ones return None."""
    event_type = payload.get("type") or ""
    actor = payload.get("actor") or {}
    occurred = parse_ts(payload.get("created_at"))
    if occurred is None:
        return None
    issue = payload.get("issue") or {}
    pull = payload.get("pull_request") or {}
    action = payload.get("action") or ""
    kind: str | None = None
    subject_type: str | None = None
    subject_number: int | None = None
    if event_type == "IssuesEvent" and action in {"opened", "closed", "reopened"}:
        kind = f"issue_{action}"
        subject_type = "issue"
        subject_number = issue.get("number")
    elif event_type == "PullRequestEvent" and action in {"opened", "closed", "reopened"}:
        if action == "closed" and pull.get("merged"):
            kind = "pr_merged"
        else:
            kind = f"pr_{action}"
        subject_type = "pull_request"
        subject_number = pull.get("number")
    elif event_type == "PullRequestReviewEvent" and action == "created":
        kind = "review_submitted"
        subject_type = "pull_request"
        subject_number = pull.get("number")
    if kind is None:
        return None
    return {
        "source_event_id": str(payload.get("id")),
        "kind": kind,
        "actor_login": actor.get("login"),
        "subject_type": subject_type,
        "subject_number": subject_number,
        "occurred_at": occurred,
    }
