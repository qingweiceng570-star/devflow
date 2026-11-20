import copy
import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
evaluation = SimpleNamespace(**runpy.run_path(str(ROOT / "scripts/evaluate_real.py")))


def cases():
    return evaluation.load_dataset(ROOT / "evals/real-v1")[1]


def test_frozen_baseline_still_scores_and_counts_repositories():
    source = cases()
    review = json.loads((ROOT / "evals/real-v1/reference-review.json").read_text(encoding="utf-8"))
    source[0]["repository"] = "another/public-repository"
    report = evaluation.summarize({"version": "test"}, source, review)
    assert report["repository_count"] == 2
    assert report["provisional_factual_precision"] == 0.75
    assert report["fully_supported_citation_rate"] == 0.3
    assert report["human_review_status"] == "pending"


def test_new_outputs_start_pending_without_claiming_accuracy():
    source = cases()
    review = evaluation.review_template(source)
    report = evaluation.summarize({"version": "test"}, source, review)
    assert report["pending_findings"] == 10
    assert report["provisional_factual_precision"] is None
    assert report["fully_supported_citation_rate"] is None
    assert report["reference_verdict_accuracy"] is None
    assert report["review_complete"] is False


def test_empty_findings_and_uncertain_reviews_do_not_divide_by_zero():
    case = cases()[0]
    case["output"]["findings"] = []
    report = evaluation.summarize({"version": "test"}, [case], evaluation.review_template([case]))
    assert report["provisional_false_discovery_rate"] is None
    assert report["fully_supported_citation_rate"] is None


def test_changed_output_or_citation_cannot_reuse_old_review():
    case = cases()[0]
    review = evaluation.review_template([case])["cases"][case["id"]]
    changed = copy.deepcopy(case)
    changed["output"]["findings"][0]["claim"] = "Different claim"
    with pytest.raises(ValueError, match="different output"):
        evaluation.score(changed, review)
    review["claims"][0]["cited_evidence_ids"] = []
    with pytest.raises(ValueError, match="citations differ"):
        evaluation.score(case, review)


def test_unknown_review_labels_are_rejected():
    case = cases()[0]
    review = evaluation.review_template([case])["cases"][case["id"]]
    review["claims"][0]["truth"] = "probably"
    with pytest.raises(ValueError, match="Invalid truth"):
        evaluation.score(case, review)
