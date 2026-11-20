"""Freeze previously authorized public runs and their evidence; zero network calls."""

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from sqlalchemy import select
from app.core.config import get_settings
from app.persistence.database import create_database
from app.persistence.models import (
    AgentRun,
    Evidence,
    Repository,
    Run,
    RunEvidence,
    ToolCall,
)
from app.storage.local import read_object

RUNS = [
    "5bfcc3bf-b36a-484e-a484-04b4f86fc79f",
    "3a9b49af-45d8-4d98-a828-aca34820de77",
    "72aab1ae-ee41-4a6f-afe6-869610e1876e",
]


def main():
    settings = get_settings()
    engine, factory = create_database(settings)
    folder = ROOT / "evals/real-v1"
    if (folder / "manifest.json").exists():
        raise RuntimeError(
            "The frozen real-v1 dataset already exists; create a new version rather than overwriting its baseline"
        )
    folder.mkdir(parents=True, exist_ok=True)
    manifest = {
        "version": "1.0",
        "origin": "previously_authorized_live_runs",
        "captured_at": datetime.now(UTC).isoformat(),
        "model_requests": 0,
        "cases": [],
    }
    try:
        with factory() as db:
            for index, value in enumerate(RUNS):
                run = db.get(Run, UUID(value))
                repo = db.get(Repository, run.repo_id)
                if repo.full_name != "simonw/ttok" or run.status != "completed":
                    raise RuntimeError(
                        "Only the previously authorized completed public cases may be exported"
                    )
                rows = db.scalars(
                    select(Evidence).where(
                        Evidence.id.in_(
                            select(RunEvidence.evidence_id).where(
                                RunEvidence.run_id == run.id
                            )
                        ),
                        Evidence.repo_id == repo.id,
                    )
                ).all()
                evidence = []
                for row in rows:
                    content = read_object(
                        settings.storage_root, row.locator["object_ref"]
                    )
                    if hashlib.sha256(content).hexdigest() != row.content_hash:
                        raise RuntimeError("Evidence hash mismatch")
                    evidence.append(
                        {
                            "id": str(row.id),
                            "type": row.source_type,
                            "locator": {
                                k: v
                                for k, v in row.locator.items()
                                if k != "object_ref"
                            },
                            "source_url": row.source_url,
                            "observed_at": row.observed_at.isoformat(),
                            "sha256": row.content_hash,
                            "content": content.decode("utf-8"),
                        }
                    )
                traces = db.scalars(
                    select(AgentRun).where(AgentRun.run_id == run.id)
                ).all()
                case_id = ["issue-25", "ci-37859163438", "pr-16"][index]
                case = {
                    "id": case_id,
                    "repository": repo.full_name,
                    "run_id": str(run.id),
                    "kind": run.kind,
                    "target": run.target,
                    "config": run.config_snapshot,
                    "output": run.result,
                    "evidence": evidence,
                    "agent_usage": [t.usage for t in traces],
                    "duration_seconds": (
                        run.finished_at - run.started_at
                    ).total_seconds()
                    if run.finished_at and run.started_at
                    else None,
                }
                case["prepared_target"] = [
                    t.result
                    for t in db.scalars(
                        select(ToolCall).where(
                            ToolCall.run_id == run.id,
                            ToolCall.tool == "get_analysis_target",
                        )
                    )
                ]
                raw = json.dumps(case, ensure_ascii=False, indent=2).encode()
                path = folder / (case_id + ".json")
                path.write_bytes(raw)
                manifest["cases"].append(
                    {
                        "id": case_id,
                        "file": path.name,
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    }
                )
            (folder / "manifest.json").write_text(
                json.dumps(manifest, indent=2), encoding="utf-8"
            )
        print(
            json.dumps(
                {"exported": len(RUNS), "folder": str(folder), "network_requests": 0}
            )
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
