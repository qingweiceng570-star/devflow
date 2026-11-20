from enum import StrEnum


class RepoState(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class MembershipRole(StrEnum):
    VIEWER = "viewer"
    ANALYST = "analyst"
    MAINTAINER = "maintainer"
    ADMIN = "admin"


class SyncStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class SyncResource(StrEnum):
    ISSUES = "issues"
    PULLS = "pulls"
    REVIEWS = "reviews"
    CI = "ci"
    EVENTS = "events"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RETRY_WAIT = "retry_wait"
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobKind(StrEnum):
    SYNC = "sync"
    ANALYZE = "analyze"
    INDEX = "index"
    IMPORT_DOCS = "import_docs"
    ACTION = "action"


class RunKind(StrEnum):
    REPO_HEALTH = "repo_health"
    PR_REVIEW = "pr_review"
    KNOWLEDGE_QA = "knowledge_qa"
    ISSUE_TRIAGE = "issue_triage"
    CI_DEBUG = "ci_debug"
    RELEASE_CHECK = "release_check"
    WEEKLY_REPORT = "weekly_report"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RECOVERING = "recovering"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class EvidenceUsage(StrEnum):
    COLLECTED = "collected"
    CITED = "cited"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class StopReason(StrEnum):
    BUDGET_EXHAUSTED = "budget_exhausted"
    STEP_LIMIT = "step_limit"
    TOOL_LIMIT = "tool_limit"
    HEAD_CHANGED = "head_changed"


class Coverage(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


class CiConclusion(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    SKIPPED = "skipped"
    ACTION_REQUIRED = "action_required"
    NEUTRAL = "neutral"
    STALE = "stale"
    STARTUP_FAILURE = "startup_failure"
    UNKNOWN = "unknown"


ACTIVE_JOB_STATUSES = (JobStatus.QUEUED, JobStatus.RETRY_WAIT, JobStatus.LEASED)
ACTIVE_SYNC_STATUSES = (SyncStatus.QUEUED, SyncStatus.RUNNING)
