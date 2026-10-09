from typing import Literal

from pydantic import Field

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


class SyncRequest(Contract):
    resources: list[SyncResource] = Field(
        default_factory=lambda: list(SyncResource), min_length=1, max_length=5
    )
    mode: Literal["incremental"] = "incremental"
