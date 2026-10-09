from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RepoScope(Contract):
    actor_id: UUID
    repo_id: UUID


class ToolError(Contract):
    code: str
    message: str
    retryable: bool = False


class ToolMeta(Contract):
    source: str
    observed_at: datetime
    freshness: Literal["fresh", "stale", "unknown"] = "unknown"
    truncated: bool = False
    next_cursor: str | None = None


class ToolResult(Contract):
    schema_version: Literal["1.0"] = "1.0"
    status: Literal["ok", "partial", "error"]
    data: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[UUID] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: ToolError | None = None
    meta: ToolMeta

    @model_validator(mode="after")
    def valid_error(self) -> "ToolResult":
        if (self.status == "error") != (self.error is not None):
            raise ValueError("Only error results must include error")
        return self


class Evidence(Contract):
    id: UUID
    repo_id: UUID
    source_type: str
    source_url: str | None = None
    observed_at: datetime
    content_hash: str
    excerpt: str
    truncated: bool = False
    commit_sha: str | None = None
    file_path: str | None = None
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def valid_lines(self) -> "Evidence":
        if (self.line_start is None) != (self.line_end is None):
            raise ValueError("Both line boundaries are required")
        if self.line_start and self.line_end and self.line_end < self.line_start:
            raise ValueError("End line must not precede start line")
        return self


class Finding(Contract):
    severity: Literal["critical", "high", "medium", "low", "info"]
    claim: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[UUID] = Field(min_length=1)
    location: str = Field(min_length=1, max_length=500)
    suggestion: str = Field(min_length=1, max_length=2000)


class Hypothesis(Contract):
    claim: str = Field(min_length=1, max_length=2000)
    supporting_evidence_ids: list[UUID] = Field(default_factory=list)
    counter_evidence_ids: list[UUID] = Field(default_factory=list)
    verification_steps: list[str] = Field(default_factory=list)


class MissingEvidence(Contract):
    item: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=500)
    impact: str = Field(min_length=1, max_length=500)
    next_action: str = Field(min_length=1, max_length=500)


class NextAction(Contract):
    description: str = Field(min_length=1, max_length=500)
    owner_hint: str | None = Field(default=None, max_length=120)
    draft_id: UUID | None = None


class AnalysisResult(Contract):
    schema_version: Literal["1.0"] = "1.0"
    kind: Literal[
        "repo_health",
        "issue_triage",
        "pr_review",
        "ci_debug",
        "release_check",
        "knowledge_qa",
        "weekly_report",
    ]
    summary: str = Field(min_length=1)
    verdict: Literal[
        "recommend_merge", "hold", "insufficient_evidence", "recommend_release", "not_applicable"
    ]
    coverage: Literal["complete", "partial", "unavailable"]
    freshness: Literal["fresh", "stale", "unknown"]
    findings: list[Finding] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    missing_evidence: list[MissingEvidence] = Field(default_factory=list)
    next_actions: list[NextAction] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    scope: RepoScope

    @model_validator(mode="after")
    def verdict_matches_kind(self) -> "AnalysisResult":
        allowed = {"not_applicable"}
        if self.kind == "pr_review":
            allowed = {"recommend_merge", "hold", "insufficient_evidence"}
        elif self.kind == "release_check":
            allowed = {"recommend_release", "hold", "insufficient_evidence"}
        if self.verdict not in allowed:
            raise ValueError("Verdict is invalid for this analysis kind")
        if self.verdict in {"recommend_merge", "recommend_release"} and (
            self.coverage != "complete" or self.freshness != "fresh"
        ):
            raise ValueError("Positive recommendations require complete, fresh evidence")
        return self
