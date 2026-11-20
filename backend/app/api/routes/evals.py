"""Read-only eval artifacts for the Evals page.

Only fixed files under ``evals/results`` are exposed; callers cannot choose a
path. The payload keeps the disclaimer fields (model_requests,
model_quality_evaluated, human_review_status) so the UI cannot present a
deterministic suite as a model-quality result.
"""

import json

from fastapi import APIRouter, Depends

from app.api.deps import current_identity
from app.core.config import PROJECT_ROOT
from app.core.errors import DomainError
from app.schemas.responses import envelope

router = APIRouter()

RESULTS = PROJECT_ROOT / "evals" / "results"
OFFLINE_FILE = "offline-latest.json"
REAL_FILE = "real-baseline-v1.json"


def _load(name: str) -> dict | None:
    path = RESULTS / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DomainError("EVAL_UNAVAILABLE", "评测结果暂时无法读取", 503) from None


def _offline(payload: dict) -> dict:
    return {
        "mode": payload.get("mode"),
        "model_quality_evaluated": payload.get("model_quality_evaluated"),
        "dataset_version": payload.get("dataset_version"),
        "dataset_sha256": payload.get("dataset_sha256"),
        "created_at": payload.get("created_at"),
        "passed": payload.get("passed"),
        "total": payload.get("total"),
        "model_requests": payload.get("model_requests"),
        "groups": payload.get("groups") or {},
        "rows": [
            {
                "id": row.get("id"),
                "split": row.get("split"),
                "group": row.get("group"),
                "passed": row.get("passed"),
                "actual": row.get("actual"),
                "duration_ms": row.get("duration_ms"),
            }
            for row in (payload.get("rows") or [])[:200]
        ],
    }


def _real(payload: dict) -> dict:
    keys = (
        "mode", "dataset_version", "sample_count", "repository_count", "split",
        "reviewer_type", "human_review_status", "model_requests", "findings",
        "adjudicated_findings", "uncertain_findings", "provisional_factual_precision",
        "provisional_false_discovery_rate", "fully_supported_citation_rate",
        "reference_verdict_accuracy", "median_duration_seconds",
        "total_recorded_tokens", "recall", "recall_note", "generalization_claim",
    )
    view = {key: payload.get(key) for key in keys}
    view["cases"] = [
        {
            "case": case.get("case"),
            "kind": case.get("kind"),
            "findings": case.get("findings"),
            "truth": case.get("truth"),
        }
        for case in (payload.get("cases") or [])[:50]
    ]
    return view


@router.get("/evals/latest")
def latest_evals(identity=Depends(current_identity)):
    offline = _load(OFFLINE_FILE)
    real = _load(REAL_FILE)
    if offline is None and real is None:
        raise DomainError("EVAL_NOT_FOUND", "还没有生成评测结果", 404)
    return envelope(
        {
            "offline": _offline(offline) if offline else None,
            "real": _real(real) if real else None,
        }
    )
