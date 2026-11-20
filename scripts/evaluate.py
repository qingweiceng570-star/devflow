"""Offline fixed-set regression. Never invokes a model or sends repository content."""
import hashlib
import json
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from pydantic import ValidationError
from app.agents.knowledge_qa import KnowledgeDraft
from app.agents.pr_review import filter_evidence_refs
from app.domain.policies import VerdictFacts, enforce_pr_verdict
from app.ingestion.ci_logs import parse_log
from app.rag.bm25_store import Bm25Index
from app.workflows.contracts import ReleaseTarget


def evaluate(case):
    data = case["input"]
    if case["group"] == "pr":
        verdict = enforce_pr_verdict(data["suggested"], VerdictFacts(**data["facts"]))[0]
        return {"passed": verdict == case["expected"]["verdict"], "actual": verdict}
    if case["group"] == "ci":
        result = parse_log(data["log"].encode(), truncated=data.get("truncated", False), known_secrets=data.get("known_secrets", []))
        text = "\n".join(b["text"] for b in result["blocks"])
        expected = case["expected"]
        passed = all(value in text for value in expected.get("contains", [])) and all(value not in text for value in expected.get("absent", []))
        passed &= len(result["blocks"]) >= expected.get("min_blocks", 0)
        if "truncated" in expected:
            passed &= result["truncated"] == expected["truncated"]
        return {"passed": bool(passed), "blocks": len(result["blocks"])}
    if case["group"] == "issue":
        draft = KnowledgeDraft.model_validate(data["draft"])
        filtered, notes = filter_evidence_refs(draft, {UUID(e) for e in data["valid_evidence"]})
        refs = [str(e) for f in filtered.findings for e in f.evidence_ids]
        return {"passed": refs == case["expected"]["citations"], "retained_citations": refs, "notes": len(notes)}
    if case["group"] == "knowledge":
        index = Bm25Index.build([(d["id"], d["text"]) for d in data["documents"]])
        hits = index.search(data["question"], 3, allowed_ids=set(data["allowed_ids"]))
        ids = [h.chunk_id for h in hits]
        wanted = set(case["expected"]["relevant_ids"])
        recall = len(wanted & set(ids)) / len(wanted) if wanted else None
        mrr = next((1 / (i + 1) for i, value in enumerate(ids) if value in wanted), 0) if wanted else None
        return {"passed": recall == 1 if wanted else ids == [], "recall_at_3": recall, "mrr": mrr, "hits": ids}
    try:
        ReleaseTarget.model_validate(data)
        valid = True
    except ValidationError:
        valid = False
    return {"passed": valid == case["expected"]["valid"], "accepted": valid}


def main():
    raw = (ROOT / "evals/dataset-v1.json").read_bytes()
    dataset = json.loads(raw)
    rows = []
    for case in dataset["cases"]:
        start = time.perf_counter()
        result = evaluate(case)
        rows.append({"id": case["id"], "split": case["split"], "group": case["group"], **result, "duration_ms": round((time.perf_counter()-start)*1000, 3)})
    git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    code = hashlib.sha256()
    sources = sorted((ROOT / "backend/app").rglob("*.py")) + [Path(__file__)]
    for path in sources:
        code.update(path.relative_to(ROOT).as_posix().encode() + b"\0" + path.read_bytes())
    report = {"mode": "deterministic_offline", "model_quality_evaluated": False, "dataset_version": dataset["version"], "dataset_sha256": hashlib.sha256(raw).hexdigest(), "code_commit": git.stdout.strip(), "working_tree_may_be_dirty": True, "created_at": datetime.now(UTC).isoformat(), "groups": dict(Counter(r["group"] for r in rows)), "passed": sum(r["passed"] for r in rows), "total": len(rows), "model_requests": 0, "rows": rows}
    output = ROOT / "evals/results/offline-latest.json"
    report["source_sha256"] = code.hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, ensure_ascii=False))
    if report["passed"] != report["total"]:
        print("Failed:", [r["id"] for r in rows if not r["passed"]])
        raise SystemExit(1)


if __name__ == "__main__":
    main()
