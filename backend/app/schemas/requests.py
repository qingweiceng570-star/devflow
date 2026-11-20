from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.domain.contracts import Contract
from app.domain.enums import SyncResource
from app.workflows.contracts import ReleaseTarget, WeeklyTarget


class LoginRequest(Contract):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=128)


class ProbeRequest(Contract):
    source: Literal["configured", "fixture"] = "fixture"
    case_id: Literal["normal_pr", "risk_pr", "failed_ci"] = "normal_pr"


class RegisterRepoRequest(Contract):
    # Accepts either owner/repo or a pasted GitHub link; the service normalizes
    # it. The bound leaves room for deep links while still limiting input size.
    full_name: str = Field(min_length=3, max_length=2048)
    credential_ref: str = Field(default="github-default", min_length=1, max_length=80)


class PatchRepoRequest(Contract):
    expected_version: int = Field(ge=1)
    state: Literal["active", "disabled"] | None = None
    required_checks: list[str] | None = Field(default=None, max_length=20)


class SyncRequest(Contract):
    resources: list[SyncResource] = Field(
        default_factory=lambda: list(SyncResource), min_length=1, max_length=5
    )
    mode: Literal["incremental"] = "incremental"


class CreateConversationRequest(Contract):
    repo_id: UUID
    title: str | None = Field(default=None, max_length=200)


class PrReviewTarget(Contract):
    pr_number: int = Field(ge=1)


class IssueTarget(Contract):
    issue_number: int = Field(ge=1)


class CiTarget(Contract):
    ci_run_id: int = Field(ge=1)
    attempt: int = Field(ge=1)
    job_id: int | None = Field(default=None, ge=1)


class SubmitRunRequest(Contract):
    message: str = Field(min_length=1, max_length=64000)
    kind: Literal["repo_health", "pr_review", "knowledge_qa", "issue_triage", "ci_debug", "release_check", "weekly_report"] = "pr_review"
    target: PrReviewTarget | IssueTarget | CiTarget | ReleaseTarget | WeeklyTarget | None = None

    @model_validator(mode="before")
    @classmethod
    def parse_target(cls, data):
        schemas = {"pr_review": PrReviewTarget, "issue_triage": IssueTarget, "ci_debug": CiTarget, "release_check": ReleaseTarget, "weekly_report": WeeklyTarget}
        if isinstance(data, dict) and isinstance(data.get("target"), dict) and data.get("kind", "pr_review") in schemas:
            data = {**data, "target": schemas[data.get("kind", "pr_review")].model_validate(data["target"])}
        return data

    @model_validator(mode="after")
    def require_target(self) -> "SubmitRunRequest":
        expected = {"pr_review": PrReviewTarget, "issue_triage": IssueTarget, "ci_debug": CiTarget, "release_check": ReleaseTarget, "weekly_report": WeeklyTarget}
        if self.kind in expected and not isinstance(self.target, expected[self.kind]):
            raise ValueError("分析类型与目标参数不匹配")
        if self.kind in {"repo_health", "knowledge_qa"} and self.target is not None:
            raise ValueError("knowledge_qa 不接受对象目标")
        return self


class DeleteDocumentRequest(Contract):
    expected_version: int = Field(ge=1)


class KnowledgeSearchRequest(Contract):
    query: str = Field(min_length=1, max_length=512)
    source_types: list[Literal["source", "generated"]] = Field(default_factory=list, max_length=4)
    date_from: str | None = Field(default=None, max_length=30)
    date_to: str | None = Field(default=None, max_length=30)


class ReindexRequest(Contract):
    reason: str | None = Field(default=None, max_length=200)
