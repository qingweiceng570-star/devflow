"""M1 acceptance helper: read a file at a pinned SHA and capture evidence.

Usage (from the project root, backend venv active in backend/):
    backend/.venv/Scripts/python.exe scripts/m1_demo_evidence.py <owner/repo> [path]

The script resolves a commit SHA from a synced pull request when one exists,
otherwise it asks the remote for the default-branch head (git protocol, which
does not consume REST quota). It then reads the file through the pinned-SHA
workspace and records an evidence row that can be fetched via
GET /api/v1/evidence/{evidence_id}.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.persistence.database import create_database  # noqa: E402
from app.persistence.models import PullRequest, Repository  # noqa: E402
from app.services.evidence_service import capture  # noqa: E402
from app.workspace.manager import WorkspaceManager  # noqa: E402
from app.workspace.reader import read_file  # noqa: E402


def resolve_sha(full_name: str, db) -> tuple[str, str]:
    repo = db.scalar(select(Repository).where(Repository.full_name == full_name))
    if repo is not None:
        pr = db.scalar(
            select(PullRequest)
            .where(PullRequest.repo_id == repo.id, PullRequest.head_sha.is_not(None))
            .order_by(PullRequest.source_updated_at.desc())
            .limit(1)
        )
        if pr is not None:
            return pr.head_sha, f"pr#{pr.number}"
    result = subprocess.run(
        ["git", "ls-remote", f"https://github.com/{full_name}.git", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    sha = result.stdout.split()[0]
    return sha, "default-branch-head"


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: m1_demo_evidence.py <owner/repo> [path]")
        return 2
    full_name = sys.argv[1]
    path = sys.argv[2] if len(sys.argv) > 2 else "README.md"
    settings = get_settings()
    engine, factory = create_database(settings)
    try:
        with factory() as db:
            repo = db.scalar(select(Repository).where(Repository.full_name == full_name))
            if repo is None:
                print(json.dumps({"error": f"repository {full_name} is not registered"}, indent=2))
                return 1
            sha, source = resolve_sha(full_name, db)
            manager = WorkspaceManager(settings, token="")
            slice_ = read_file(settings, manager, repo, sha, path)
            evidence = capture(
                db,
                settings,
                repo_id=repo.id,
                source_type="code",
                content=slice_.text,
                source_url=f"https://github.com/{full_name}/blob/{sha}/{path}",
                locator={
                    "commit_sha": sha,
                    "path": path,
                    "line_start": slice_.line_start,
                    "line_end": slice_.line_end,
                },
            )
            db.commit()
            print(
                json.dumps(
                    {
                        "evidence_id": str(evidence.id),
                        "repo": full_name,
                        "sha_source": source,
                        "commit_sha": sha,
                        "path": path,
                        "line_range": [slice_.line_start, slice_.line_end],
                        "total_lines": slice_.total_lines,
                        "truncated": slice_.truncated,
                        "content_hash": evidence.content_hash,
                        "excerpt": evidence.excerpt[:200],
                        "verify_url": f"/api/v1/evidence/{evidence.id}",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
