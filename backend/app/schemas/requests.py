from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.domain.contracts import Contract
from app.domain.enums import SyncResource


class LoginRequest(Contract):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=128)


class ProbeRequest(Contract):
    source: Literal["configured", "fixture"] = "fixture"
    case_id: Literal["normal_pr", "risk_pr", "failed_ci"] = "normal_pr"


class RegisterRepoRequest(Contract):
    full_name: str = Field(min_length=3, max_length=201)
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


class SubmitRunRequest(Contract):
    message: str = Field(min_length=1, max_length=64000)
    kind: Literal["pr_review"] = "pr_review"
    target: PrReviewTarget | None = None

    @model_validator(mode="after")
    def require_target(self) -> "SubmitRunRequest":
        if self.kind == "pr_review" and self.target is None:
            raise ValueError("pr_review 需要 target.pr_number")
        return self
