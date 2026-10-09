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
