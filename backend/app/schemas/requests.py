from typing import Literal

from pydantic import Field

from app.domain.contracts import Contract


class LoginRequest(Contract):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=128)


class ProbeRequest(Contract):
    source: Literal["configured", "fixture"] = "fixture"
    case_id: Literal["normal_pr", "risk_pr", "failed_ci"] = "normal_pr"
