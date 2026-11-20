"""Explicit live M4 smoke run. Uses configured provider and its normal API quota.

Creates two local conversations only; GitHub calls are read-only. Never prints
credentials, signed log URLs or raw logs. Results go to var/m4-live-results.json.
"""
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import get_settings
from app.ingestion.github_sync import create_read_client
from app.integrations.github.credentials import resolve_read_token
from app.integrations.github.read_adapter import GitHubRead


def main():
    settings = get_settings()
    credentials = json.loads((ROOT / "var/initial-admin.json").read_text(encoding="utf-8-sig"))
    with httpx.Client(base_url="http://127.0.0.1:8100/api/v1", timeout=30, trust_env=False,
                      headers={"Origin": settings.app_origin}) as client:
        response = client.post("/auth/login", json=credentials)
        response.raise_for_status()
        client.headers["X-CSRF-Token"] = client.cookies.get("devflow_csrf")
        repos = client.get("/repos").json()["data"]["items"]
        repo = next((r for r in repos if r["full_name"] == "simonw/ttok"), None)
        if not repo:
            raise RuntimeError("Expected previously registered simonw/ttok repository")
        read = GitHubRead(create_read_client(settings, resolve_read_token(settings, "github-default")), repo["full_name"])
        issues = read.list_issues("all").items
        ci = read.list_ci_runs().items
        selected = next((r for r in ci if r.get("conclusion") == "failure"), ci[0] if ci else None)
        targets = []
        if issues:
            targets.append(("issue_triage", {"issue_number": issues[0]["number"]}))
        if selected:
            targets.append(("ci_debug", {"ci_run_id": selected["id"], "attempt": selected.get("run_attempt", 1)}))
        records = []
        for kind, target in targets:
            conversation = client.post("/conversations", json={"repo_id": repo["id"], "title": f"M4 验收 {kind}"})
            conversation.raise_for_status()
            cid = conversation.json()["data"]["id"]
            response = client.post(f"/conversations/{cid}/runs", json={"kind": kind, "target": target,
                "message": "请分析这个目标；引用实际证据，区分事实与假设，明确缺失信息并提供可验证的下一步建议。"})
            response.raise_for_status()
            run_id = response.json()["data"]["id"]
            print(f"Started {kind}: {run_id}", flush=True)
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                run = client.get(f"/runs/{run_id}").json()["data"]
                if run["status"] not in {"queued", "running", "recovering"}:
                    break
                time.sleep(2)
            else:
                client.post(f"/runs/{run_id}/cancel", json={})
                raise RuntimeError("Smoke deadline reached; cancellation requested")
            result = run.get("result") or {}
            record = {"kind": kind, "target": target, "status": run["status"], "run_id": run_id,
                "conversation_id": cid, "repo_id": repo["id"], "coverage": result.get("coverage"),
                "freshness": result.get("freshness"), "findings": len(result.get("findings", [])),
                "log_blocks": len((result.get("details") or {}).get("error_blocks", [])),
                "error": run.get("error"), "limitations": result.get("limitations", [])}
            records.append(record)
            print(json.dumps(record, ensure_ascii=False), flush=True)
            (ROOT / "var/m4-live-results.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        client.post("/auth/logout", json={})


if __name__ == "__main__":
    main()
