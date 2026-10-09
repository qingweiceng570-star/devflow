"""Export actual HTTP/domain contracts without connecting to a database."""
import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "backend"))
from app.agents.probe import ProbeAnswer
from app.agents.pr_review import PRReviewDraft
from app.core.config import Settings
from app.domain.contracts import (
    AnalysisResult,
    Evidence,
    Finding,
    Hypothesis,
    MissingEvidence,
    NextAction,
    RepoScope,
    ToolResult,
)
from app.main import create_app

settings = Settings(_env_file=None, app_env="test", database_url="sqlite://")
output = root / "contracts"
output.mkdir(exist_ok=True)
app = create_app(settings, session_factory=lambda: None)
(output / "openapi.json").write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2), encoding="utf-8")
(output / "domain-schemas.json").write_text(json.dumps({
    model.__name__: model.model_json_schema() for model in [
        RepoScope, ToolResult, Evidence, Finding, Hypothesis, MissingEvidence, NextAction,
        AnalysisResult, PRReviewDraft, ProbeAnswer,
    ]
}, ensure_ascii=False, indent=2), encoding="utf-8")
print("Exported OpenAPI and domain JSON schemas.")
