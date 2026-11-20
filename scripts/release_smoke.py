"""User-authorized read-only public-repo acceptance, at most two child analyses."""
import json
import sys
import time
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.core.config import get_settings


def main():
    settings = get_settings()
    credentials = json.loads((ROOT / "var/initial-admin.json").read_text(encoding="utf-8-sig"))
    with httpx.Client(base_url="http://127.0.0.1:8100/api/v1", timeout=30, trust_env=False, headers={"Origin": settings.app_origin}) as client:
        response = client.post("/auth/login", json=credentials)
        response.raise_for_status()
        client.headers["X-CSRF-Token"] = client.cookies.get("devflow_csrf")
        repos = client.get("/repos").json()["data"]["items"]
        repo = next(r for r in repos if r["full_name"] == "simonw/ttok")
        response = client.post("/conversations", json={"repo_id": repo["id"], "title": "M5 真实发布检查 PR #16"})
        response.raise_for_status()
        conversation_id = response.json()["data"]["id"]
        response = client.post(f"/conversations/{conversation_id}/runs", json={"kind": "release_check", "target": {"pr_number": 16, "observer_rounds": 0}, "message": "检查 PR #16；只读分析，缺少同提交 CI 或发布策略时明确保留判断。"})
        response.raise_for_status()
        run_id = response.json()["data"]["id"]
        print(json.dumps({"run_id": run_id, "conversation_id": conversation_id}), flush=True)
        deadline = time.monotonic() + 660
        while time.monotonic() < deadline:
            result = client.get(f"/runs/{run_id}").json()["data"]
            if result["status"] not in {"queued", "running", "recovering"}:
                break
            time.sleep(2)
        else:
            client.post(f"/runs/{run_id}/cancel", json={})
            raise RuntimeError("Deadline reached; cancellation requested")
        tasks = client.get(f"/repos/{repo['id']}/runs/{run_id}/tasks").json()["data"]
        record = {"run_id": run_id, "conversation_id": conversation_id, "repo_id": repo["id"], "status": result["status"], "error": result.get("error"), "result": result.get("result"), "tasks": tasks}
        (ROOT / "var/release-live-result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": record["status"], "error": record["error"], "tasks": [{"agent": t["agent"], "state": t["state"]} for t in tasks], "verdict": (result.get("result") or {}).get("verdict")}), flush=True)
        client.post("/auth/logout", json={})


if __name__ == "__main__":
    main()
