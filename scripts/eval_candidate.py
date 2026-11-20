"""Capture public PR evidence or replay frozen evidence with the configured model.

Replay tests current prompts with fixed evidence, not live ingestion or end-to-end
verdict policy. Candidate results and pending human reviews live in a new folder.
"""

import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from evaluate_real import digest, load_dataset, review_template

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.agents.pr_review import PRReviewDraft, build_system_prompt
from app.agents.runtime import AgentLoop, LoopPolicy
from app.agents.specialists import CIDraft, IssueDraft, prompt
from app.context.budgeter import estimate_tokens
from app.core.config import Settings
from app.core.errors import DomainError
from app.domain.contracts import ToolMeta, ToolResult
from app.integrations.github.client import GitHubClient
from app.integrations.github.read_adapter import GitHubRead
from app.integrations.llm.adapter import CompatibleModel


def write_case(folder, case):
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", case["id"]) + ".json"
    raw = json.dumps(case, ensure_ascii=False, indent=2).encode()
    with (folder / name).open("xb") as file:
        file.write(raw)
    return {"id": case["id"], "file": name, "sha256": hashlib.sha256(raw).hexdigest()}


def write_manifest(folder, entries, **metadata):
    manifest = {"version": "2.0", "captured_at": datetime.now(UTC).isoformat(),
                "split": "development", "cases": entries, **metadata}
    with (folder / "manifest.json").open("x", encoding="utf-8") as file:
        json.dump(manifest, file, ensure_ascii=False, indent=2)


def capture(args):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository) or args.pr < 1:
        raise ValueError("Expected owner/repository and a positive PR number")
    settings = Settings()
    read = GitHubRead(GitHubClient(settings, token=""), args.repository)
    repo = read.get_repo()
    if repo.get("private") or repo.get("full_name", "").lower() != args.repository.lower():
        raise ValueError("Only an explicitly named public repository may be captured")
    pr = read.get_pr_live(args.pr)
    head, base = pr["head"]["sha"], pr["base"]["sha"]
    observed = datetime.now(UTC).isoformat()
    evidence = []

    def add(kind, source, content, **locator):
        sha = hashlib.sha256(content.encode()).hexdigest()
        evidence.append({"id": str(uuid5(NAMESPACE_URL, source + sha)), "type": kind,
                         "source_url": source, "observed_at": observed, "sha256": sha,
                         "locator": {"head_sha": head, **locator}, "content": content})

    source = f"https://github.com/{args.repository}/pull/{args.pr}"
    facts = {key: pr.get(key) for key in ("number", "title", "body", "state", "merged", "mergeable", "changed_files")}
    facts.update(head_sha=head, base_sha=base)
    add("pull_request", source, json.dumps(facts, ensure_ascii=False))
    page = read.list_pr_files(args.pr)
    if page.next_cursor or len(page.items) != pr["changed_files"]:
        raise ValueError("Capture requires a complete single-page PR diff")
    for item in page.items:
        if not item.get("patch"):
            raise ValueError("Missing patch; choose a PR with fully available text diffs")
        add("pr_diff", source + "/files", item["filename"] + "\n" + item["patch"], path=item["filename"])
    if read.get_pr_live(args.pr)["head"]["sha"] != head:
        raise ValueError("PR changed during collection; discard this capture")
    case = {"id": args.repository.replace("/", "-") + f"-pr-{args.pr}",
            "repository": args.repository, "kind": "pr_review",
            "target": {"pr_number": args.pr, "head_sha": head, "base_sha": base},
            "evidence": evidence, "prepared_target": [],
            "limitations": ["Historical PR metadata; current CI, complete code context and required checks are unavailable."]}
    args.output.mkdir(parents=True, exist_ok=False)
    write_manifest(args.output, [write_case(args.output, case)], origin="public_github_snapshot", model_requests=0)
    print(json.dumps({"captured": case["id"], "evidence_count": len(evidence), "head_sha": head}))


class FrozenTools:
    def __init__(self, case):
        self.case, self.has_evidence = case, False
        self.ids = {UUID(e["id"]) for e in case["evidence"]}

    @property
    def schemas(self):
        return [{"type": "function", "function": {
            "name": "get_analysis_target", "description": "Read the complete frozen target and evidence snapshot.",
            "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            "strict": True,
        }}]

    async def execute(self, name, arguments):
        if name != "get_analysis_target" or json.loads(arguments) != {}:
            raise DomainError("INVALID_TOOL_ARGUMENTS", "Only the frozen target is available")
        self.has_evidence = True
        return ToolResult(
            status="partial", evidence_ids=sorted(self.ids, key=str),
            data={key: self.case.get(key, []) for key in ("target", "evidence", "prepared_target", "limitations")},
            warnings=["FROZEN_EVIDENCE_ONLY"],
            meta=ToolMeta(source="frozen_public_evaluation", observed_at=datetime.now(UTC)),
        )

    def validate(self, draft):
        if not self.has_evidence:
            raise ValueError("Read the snapshot before answering")
        for finding in draft.findings:
            if not set(finding.evidence_ids) <= self.ids:
                raise ValueError("Every finding must cite evidence from the snapshot")


class MeteredModel:
    def __init__(self, settings, budget):
        self.delegate = CompatibleModel(settings)
        self.budget, self.total, self.calls = budget, 0, 0

    async def complete(self, request):
        estimate = estimate_tokens(json.dumps(request.messages, ensure_ascii=False))
        estimate += estimate_tokens(json.dumps(request.tools)) + request.max_output_tokens
        if self.total + estimate > self.budget:
            raise DomainError("EVAL_BUDGET_EXCEEDED", "Evaluation token budget exhausted")
        self.calls += 1
        reply = await self.delegate.complete(request)
        self.total += reply.input_tokens + reply.output_tokens
        return reply


async def replay(args):
    cases = [case for folder in args.dataset for case in load_dataset(folder)[1]]
    if not 1 <= len(cases) <= args.max_cases or len({c["id"] for c in cases}) != len(cases):
        raise ValueError("Too many cases or duplicate case IDs")
    settings = Settings().model_copy(update={"model_timeout_seconds": 60, "agent_timeout_seconds": 180,
                                             "agent_max_steps": 4, "agent_total_token_budget": 24000})
    if not args.live:
        print(json.dumps({"dry_run": True, "cases": [c["id"] for c in cases], "model": settings.llm_model,
                          "max_total_tokens": args.max_total_tokens, "note": "Pass --live to call the configured model."}))
        return
    if not settings.model_configured or settings.llm_provider != "openai_compatible":
        raise ValueError("A real configured model is required")
    # Fail before calling the model if any frozen evidence would exceed the tool bound.
    for case in cases:
        payload = {key: case.get(key, []) for key in ("target", "evidence", "prepared_target", "limitations")}
        if len(json.dumps(payload, ensure_ascii=False).encode()) > settings.tool_result_max_bytes - 4096:
            raise ValueError(f"Frozen evidence exceeds tool size limit: {case['id']}")
    args.output.mkdir(parents=True, exist_ok=False)
    model = MeteredModel(settings, args.max_total_tokens)
    entries, candidates, failures = [], [], []
    for case in cases:
        started, used = time.monotonic(), model.total
        try:
            if case["kind"] == "pr_review":
                system, schema = build_system_prompt(case["repository"], []), PRReviewDraft
            elif case["kind"] in {"issue_triage", "ci_debug"}:
                system = prompt(case["kind"], case["repository"])
                schema = IssueDraft if case["kind"] == "issue_triage" else CIDraft
            else:
                raise ValueError("Unsupported evaluation kind")
            system += "\n本轮是历史证据重放。必须调用 get_analysis_target；仅使用冻结证据，不将其视为当前状态。没有其他工具。"
            tools = FrozenTools(case)
            outcome = await AgentLoop(model, tools, settings, output_model=schema,
                                      policy=LoopPolicy(tool_choice="required_until_evidence"),
                                      validate_output=tools.validate).run([
                {"role": "system", "content": system},
                {"role": "user", "content": "分析固定目标，逐条核查事实与引用。信息不足时明确保留未知。"},
            ])
            output = outcome.parsed.model_dump(mode="json")
            if case["kind"] == "pr_review":
                if output["verdict"] == "recommend_merge":
                    output["verdict"] = "insufficient_evidence"
            else:
                output["verdict"] = "not_applicable"
            candidate = {key: case[key] for key in ("id", "repository", "kind", "target", "evidence")}
            candidate.update(output=output, prepared_target=case.get("prepared_target", []),
                             duration_seconds=round(time.monotonic() - started, 3),
                             agent_usage=[{"total_tokens": model.total - used, "estimated": outcome.usage_estimated}],
                             config={"model": settings.llm_model, "prompt_sha256": digest(system),
                                     "source_case_sha256": digest(case), "mode": "frozen_evidence_replay"})
            entries.append(write_case(args.output, candidate))
            candidates.append(candidate)
            print(json.dumps({"case": case["id"], "status": "completed", "tokens": model.total - used}), flush=True)
        except (DomainError, ValueError) as error:
            failures.append({"case": case["id"], "error": error.code if isinstance(error, DomainError) else type(error).__name__})
            print(json.dumps(failures[-1]), flush=True)
    write_manifest(args.output, entries, origin="frozen_evidence_replay", model_requests=model.calls,
                   total_tokens=model.total, failures=failures)
    with (args.output / "reference-review.json").open("x", encoding="utf-8") as file:
        json.dump(review_template(candidates), file, ensure_ascii=False, indent=2)
    if failures:
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture_parser = commands.add_parser("capture")
    capture_parser.add_argument("--repository", required=True)
    capture_parser.add_argument("--pr", type=int, required=True)
    capture_parser.add_argument("--output", type=Path, required=True)
    replay_parser = commands.add_parser("replay")
    replay_parser.add_argument("--dataset", type=Path, action="append", required=True)
    replay_parser.add_argument("--output", type=Path, required=True)
    replay_parser.add_argument("--live", action="store_true")
    replay_parser.add_argument("--max-cases", type=int, default=5)
    replay_parser.add_argument("--max-total-tokens", type=int, default=60000)
    args = parser.parse_args()
    if args.command == "capture":
        capture(args)
    else:
        asyncio.run(replay(args))


if __name__ == "__main__":
    main()
