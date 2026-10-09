import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid5

from pydantic import ValidationError

from app.core.config import PROJECT_ROOT
from app.domain.contracts import Contract, RepoScope, ToolError, ToolMeta, ToolResult

FIXTURE_REPO_ID = UUID("00000000-0000-4000-8000-000000000001")


class SampleInput(Contract):
    case_id: Literal["normal_pr", "risk_pr", "failed_ci"]


class ToolRegistry:
    def __init__(self, scope: RepoScope, fixture_root: Path | None = None):
        self.scope = scope
        self.fixture_root = fixture_root or PROJECT_ROOT / "fixtures" / "github"
        self.successful_evidence: dict[str, str] = {}

    @property
    def schemas(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "read_sample",
                    "description": "Read a fixed synthetic development sample, not a live repository.",
                    "parameters": SampleInput.model_json_schema(),
                    "strict": True,
                },
            }
        ]

    async def execute(self, name: str, raw_arguments: str) -> ToolResult:
        meta = ToolMeta(
            source="synthetic_fixture", observed_at=datetime.now(UTC), freshness="unknown"
        )
        if self.scope.repo_id != FIXTURE_REPO_ID:
            return ToolResult(
                status="error",
                error=ToolError(code="FORBIDDEN", message="样例工具不允许访问真实仓库"),
                meta=meta,
            )
        if name != "read_sample":
            return ToolResult(
                status="error",
                error=ToolError(code="UNKNOWN_TOOL", message="工具未注册"),
                meta=meta,
            )
        try:
            args = SampleInput.model_validate_json(raw_arguments)
        except ValidationError:
            return ToolResult(
                status="error",
                error=ToolError(code="INVALID_TOOL_ARGUMENTS", message="参数不符合工具契约"),
                meta=meta,
            )
        path = self.fixture_root / (args.case_id + ".json")
        data = json.loads(path.read_text(encoding="utf-8"))
        encoded = json.dumps(data, sort_keys=True, ensure_ascii=False)
        evidence_id = str(uuid5(FIXTURE_REPO_ID, hashlib.sha256(encoded.encode()).hexdigest()))
        self.successful_evidence[evidence_id] = data["verification_value"]
        return ToolResult(status="ok", data=data, evidence_ids=[UUID(evidence_id)], meta=meta)
