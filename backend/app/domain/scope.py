from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class ExecutionContext:
    """Server-built execution scope (README 6.2).

    Tools receive this object plus validated model arguments; the model can
    never alter actor, repository, credentials, workspace root or budget.
    """

    actor_id: UUID
    repo_id: UUID
    credential_ref: str
    run_id: UUID | None = None
    conversation_id: UUID | None = None
    task_run_id: UUID | None = None
    pr_number: int | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    ci_run_external_id: str | None = None
    deadline_at: datetime | None = None
    prompt_version: str = ""
    toolset_version: str = ""
    config_snapshot: dict = field(default_factory=dict)
    lease_epoch: int = 0
