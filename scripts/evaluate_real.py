"""Offline scoring of immutable live cases and explicit claim review ledgers."""

import argparse
import hashlib
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def score(case, review):
    if review["output_sha256"] != digest(case["output"]):
        raise ValueError(
            "Review refers to a different output; review the changed claims before scoring"
        )
    valid = set()
    for item in case["evidence"]:
        if hashlib.sha256(item["content"].encode()).hexdigest() != item["sha256"]:
            raise ValueError("Evidence hash mismatch")
        valid.add(item["id"])
    findings = case["output"]["findings"]
    if sorted(r["finding"] for r in review["claims"]) != list(range(len(findings))):
        raise ValueError("Every finding needs exactly one review decision")
    for finding, decision in zip(findings, sorted(review["claims"], key=lambda r: r["finding"]), strict=True):
        if decision["truth"] not in {"supported", "refuted", "uncertain", "pending"}:
            raise ValueError("Invalid truth decision")
        if decision["citation_support"] not in {"supported", "partial", "unsupported", "pending"}:
            raise ValueError("Invalid citation decision")
        if sorted(decision["cited_evidence_ids"]) != sorted(finding["evidence_ids"]):
            raise ValueError("Review citations differ from the finding")
        if decision["citation_support"] == "supported" and (
            not finding["evidence_ids"] or not set(finding["evidence_ids"]) <= valid
        ):
            raise ValueError("Supported citations must reference collected evidence")
        if decision["truth"] != "pending" and not decision.get("reason", "").strip():
            raise ValueError("A review decision needs a source-grounded reason")
    refs = [eid for f in findings for eid in f["evidence_ids"]]
    counts = {
        key: sum(r["truth"] == key for r in review["claims"])
        for key in ("supported", "refuted", "uncertain", "pending")
    }
    adjudicated = counts["supported"] + counts["refuted"]
    return {
        "case": case["id"],
        "kind": case["kind"],
        "repository": case["repository"],
        "findings": len(findings),
        "truth": counts,
        "factual_precision": counts["supported"] / adjudicated if adjudicated else None,
        "false_discovery_rate": counts["refuted"] / adjudicated if adjudicated else None,
        "fully_supported_citations": sum(
            r["citation_support"] == "supported" for r in review["claims"]
        ),
        "citation_integrity": sum(e in valid for e in refs) / len(refs)
        if refs
        else None,
        "pending_citations": sum(r["citation_support"] == "pending" for r in review["claims"]),
        "verdict_correct": case["output"]["verdict"] == review["expected_verdict"]
        if review.get("expected_verdict") is not None else None,
        "duration_seconds": case["duration_seconds"],
        "total_tokens": sum(a.get("total_tokens", 0) for a in case["agent_usage"]),
        "usage_estimated": any(a.get("estimated", False) for a in case["agent_usage"]),
    }


def load_dataset(folder):
    folder = Path(folder).resolve()
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    cases, ids = [], set()
    for entry in manifest["cases"]:
        path = (folder / entry["file"]).resolve()
        if (
            not path.is_relative_to(folder.resolve())
            or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]
        ):
            raise ValueError("Case manifest mismatch")
        case = json.loads(path.read_text(encoding="utf-8"))
        if case["id"] != entry["id"] or case["id"] in ids or not case.get("repository"):
            raise ValueError("Case identity mismatch or duplicate case")
        for item in case["evidence"]:
            if hashlib.sha256(item["content"].encode()).hexdigest() != item["sha256"]:
                raise ValueError("Evidence hash mismatch")
        ids.add(case["id"])
        cases.append(case)
    if not cases:
        raise ValueError("Dataset must contain at least one case")
    return manifest, cases


def review_template(cases):
    return {
        "reviewer_type": "unassigned", "human_review_status": "pending", "split": "development",
        "cases": {
            case["id"]: {
                "output_sha256": digest(case["output"]), "expected_verdict": None,
                "claims": [
                    {"finding": i, "truth": "pending", "citation_support": "pending",
                     "reason": "", "cited_evidence_ids": finding["evidence_ids"]}
                    for i, finding in enumerate(case["output"]["findings"])
                ],
            } for case in cases
        },
    }


def summarize(manifest, cases, reviews):
    if set(reviews["cases"]) != {case["id"] for case in cases}:
        raise ValueError("Review must cover exactly the dataset cases")
    rows = [score(case, reviews["cases"][case["id"]]) for case in cases]
    supported = sum(r["truth"]["supported"] for r in rows)
    refuted = sum(r["truth"]["refuted"] for r in rows)
    total = sum(r["findings"] for r in rows)
    verdicts = [r["verdict_correct"] for r in rows if r["verdict_correct"] is not None]
    pending = sum(r["truth"]["pending"] for r in rows)
    pending_citations = sum(r["pending_citations"] for r in rows)
    review_complete = pending == 0 and pending_citations == 0 and len(verdicts) == len(rows)
    durations = [r["duration_seconds"] for r in rows if r["duration_seconds"] is not None]
    report = {
        "mode": "real_case_offline_review",
        "dataset_version": manifest["version"],
        "sample_count": len(rows),
        "repository_count": len({c["repository"] for c in cases}),
        "repositories": sorted({c["repository"] for c in cases}),
        "split": reviews.get("split", "development"),
        "reviewer_type": reviews["reviewer_type"],
        "human_review_status": reviews["human_review_status"],
        "model_requests": 0,
        "findings": total,
        "adjudicated_findings": supported + refuted,
        "uncertain_findings": sum(r["truth"]["uncertain"] for r in rows),
        "pending_findings": pending,
        "pending_citations": pending_citations,
        "review_complete": review_complete,
        "provisional_factual_precision": supported / (supported + refuted) if supported + refuted else None,
        "provisional_false_discovery_rate": refuted / (supported + refuted) if supported + refuted else None,
        "fully_supported_citation_rate": sum(
            r["fully_supported_citations"] for r in rows
        )
        / total if total and not pending_citations else None,
        "reference_verdict_accuracy": sum(verdicts) / len(verdicts) if verdicts else None,
        "median_duration_seconds": statistics.median(durations) if durations else None,
        "total_recorded_tokens": sum(r["total_tokens"] for r in rows),
        "recall": None,
        "recall_note": "No independent exhaustive defect gold set; recall is not measurable yet",
        "generalization_claim": False,
        "cases": rows,
    }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / "evals/real-v1")
    parser.add_argument("--review", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "evals/results/real-latest.json")
    parser.add_argument("--init-review", type=Path, help="Create an unfilled review; never reuse old labels")
    parser.add_argument("--require-human-review", action="store_true")
    parser.add_argument("--min-repositories", type=int, default=1)
    args = parser.parse_args()
    folder = args.dataset.resolve()
    manifest, cases = load_dataset(folder)
    if args.init_review:
        with args.init_review.open("x", encoding="utf-8") as file:
            json.dump(review_template(cases), file, ensure_ascii=False, indent=2)
        print(f"Created pending review: {args.init_review}")
        return
    review_path = args.review or folder / "reference-review.json"
    reviews = json.loads(review_path.read_text(encoding="utf-8"))
    report = summarize(manifest, cases, reviews)
    gates = []
    if report["repository_count"] < args.min_repositories:
        gates.append("insufficient_repositories")
    if args.require_human_review and (
        report["human_review_status"] != "completed" or report["reviewer_type"] != "human"
        or not report["review_complete"]
    ):
        gates.append("human_review_incomplete")
    report["gate_failures"] = gates
    report["dataset_sha256"] = hashlib.sha256(
        (folder / "manifest.json").read_bytes()
    ).hexdigest()
    report["reference_review_sha256"] = hashlib.sha256(
        review_path.read_bytes()
    ).hexdigest()
    report["scorer_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    out = args.output.resolve()
    protected = {review_path.resolve(), folder / "manifest.json", ROOT / "evals/results/real-baseline-v1.json"}
    protected.update((folder / entry["file"]).resolve() for entry in manifest["cases"])
    if out in protected:
        parser.error("Output must not overwrite source cases, reviews or the frozen baseline")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {k: v for k, v in report.items() if k != "cases"}, ensure_ascii=False
        )
    )
    if gates:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
