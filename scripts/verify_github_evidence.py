"""Read-only verification on the previously authorized public simonw/ttok cases."""

import json
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main():
    credentials = json.loads(
        (ROOT / "var/initial-admin.json").read_text(encoding="utf-8-sig")
    )
    with httpx.Client(
        base_url="http://127.0.0.1:8100/api/v1",
        headers={"Origin": "http://localhost:3100"},
        trust_env=False,
        timeout=65,
    ) as client:
        response = client.post("/auth/login", json=credentials)
        response.raise_for_status()
        client.headers["X-CSRF-Token"] = client.cookies["devflow_csrf"]
        repos = client.get("/repos").json()["data"]["items"]
        repo = next(r for r in repos if r["full_name"] == "simonw/ttok")
        prefix = f"/repos/{repo['id']}"
        response = client.get(prefix + "/ci-options", params={"pr_number": 16})
        response.raise_for_status()
        ci = response.json()["data"]
        response = client.post(
            prefix + "/github-diagnostics", params={"job_id": 113590523680}, json={}
        )
        response.raise_for_status()
        report = {
            "repository": "simonw/ttok",
            "pr_number": 16,
            "ci_query": {
                "head_sha": ci["head_sha"],
                "matches": len(ci["items"]),
                "pages": ci["pages"],
                "has_more": ci["has_more"],
            },
            "diagnostics": response.json()["data"],
            "model_requests": 0,
            "github_writes": 0,
        }
        (ROOT / "var/github-evidence-verification.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
