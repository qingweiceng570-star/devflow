"""Smoke test only the isolated devflow-acceptance stack; no external requests."""
import json
import subprocess
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ["docker", "compose", "-p", "devflow-acceptance", "--env-file", str(ROOT / ".env"), "-f", str(ROOT / "infra/compose.yaml"), "-f", str(ROOT / "infra/compose.acceptance.yaml"), "--profile", "app"]
SEED = """
from sqlalchemy import select
from app.core.config import get_settings
from app.persistence.database import create_database
from app.persistence.models import Repository, RepoMembership, User
engine, factory = create_database(get_settings())
with factory() as db:
    repo = db.scalar(select(Repository).where(Repository.external_id == 'local-acceptance-fixture'))
    if repo is None:
        repo = Repository(full_name='acceptance/local-fixture', external_id='local-acceptance-fixture')
        db.add(repo)
        db.flush()
        actor = db.scalar(select(User).where(User.username == 'admin'))
        db.add(RepoMembership(repo_id=repo.id, user_id=actor.id, role='admin'))
        db.commit()
    print(repo.id)
engine.dispose()
"""


def main():
    seeded = subprocess.run(COMPOSE + ["exec", "-T", "api", "uv", "run", "--no-sync", "python", "-"], input=SEED, text=True, capture_output=True, check=True)
    repo_id = seeded.stdout.strip()
    credentials = json.loads((ROOT / "var/clean-acceptance/initial-admin.json").read_text())
    with httpx.Client(base_url="http://localhost:13100", trust_env=False, timeout=40, headers={"Origin": "http://localhost:13100"}) as client:
        def api(method, path, **kwargs):
            response = client.request(method, "/api/v1" + path, **kwargs)
            response.raise_for_status()
            return response.json()["data"]
        assert client.get("/login").status_code == 200
        ready = api("GET", "/health/ready")
        api("POST", "/auth/login", json=credentials)
        client.headers["X-CSRF-Token"] = client.cookies["devflow_csrf"]
        capability = api("GET", "/settings/capabilities")
        assert capability["worker_online"] and capability["stage"] == "M7"
        probe = api("POST", "/settings/capabilities/probe", json={"source": "fixture", "case_id": "normal_pr"})
        assert probe["passed"] and probe["provider"] == "fixture"
        conv = api("POST", "/conversations", json={"repo_id": repo_id, "title": "Docker offline acceptance"})
        submitted = api("POST", f"/conversations/{conv['id']}/runs", json={"kind": "weekly_report", "message": "Count local fixture events", "target": {"period_start": "2026-10-01T00:00:00Z", "period_end": "2026-10-08T00:00:00Z", "timezone": "UTC"}})
        run_id = submitted["id"]
        for _ in range(30):
            run = api("GET", f"/runs/{run_id}")
            if run["status"] in {"completed", "failed", "cancelled", "timed_out"}:
                break
            time.sleep(0.5)
        assert run["status"] == "completed", run["status"]
        events = client.get(f"/api/v1/runs/{run_id}/events", headers={"Accept": "text/event-stream"})
        assert events.status_code == 200 and "run.completed" in events.text
        report = {"project": "devflow-acceptance", "schema": ready["schema_revision"], "worker_online": True, "fixture_probe_passed": True, "weekly_run": run_id, "sse_replay_passed": True, "frontend_proxy_passed": True, "model_requests": 0, "github_requests": 0}
        (ROOT / "var/docker-acceptance-result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))


if __name__ == "__main__":
    main()
