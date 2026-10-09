from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.domain.contracts import AnalysisResult, RepoScope


@pytest.mark.parametrize(
    "kind,verdict",
    [
        ("ci_debug", "recommend_merge"),
        ("pr_review", "recommend_release"),
        ("release_check", "not_applicable"),
    ],
)
def test_verdict_must_match_kind(kind, verdict):
    with pytest.raises(ValidationError):
        AnalysisResult(
            kind=kind,
            summary="test",
            verdict=verdict,
            coverage="complete",
            freshness="fresh",
            scope=RepoScope(actor_id=uuid4(), repo_id=uuid4()),
        )


@pytest.mark.parametrize(
    "coverage,freshness", [("partial", "fresh"), ("complete", "stale"), ("unavailable", "unknown")]
)
def test_positive_verdict_requires_complete_fresh_evidence(coverage, freshness):
    with pytest.raises(ValidationError):
        AnalysisResult(
            kind="pr_review",
            summary="test",
            verdict="recommend_merge",
            coverage=coverage,
            freshness=freshness,
            scope=RepoScope(actor_id=uuid4(), repo_id=uuid4()),
        )
