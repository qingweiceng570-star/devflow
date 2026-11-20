"""Verify restored objects and rebuild BM25 only in an isolated restore database."""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.engine import make_url
from app.core.config import get_settings
from app.persistence.database import create_database
from app.persistence.models import Repository, SourceSnapshot
from app.rag.index_manager import build_and_activate
from app.rag.retriever import retrieve
from app.storage.local import read_object


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--storage", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"devflow_restore_[a-z0-9_]{1,40}", args.database):
        parser.error("Only an isolated devflow_restore_ database is allowed")
    original = get_settings()
    storage = args.storage.resolve()
    if storage == original.storage_root.resolve() or not (storage / "objects").is_dir():
        parser.error("Use an existing isolated restore storage directory")
    url = make_url(original.database_url.get_secret_value()).set(database=args.database)
    settings = original.model_copy(update={
        "database_url": SecretStr(url.render_as_string(hide_password=False)),
        "storage_root": storage, "milvus_uri": "", "embedding_base_url": "", "embedding_model": "",
    })
    engine, factory = create_database(settings)
    rows = []
    verified = 0
    try:
        with factory() as db:
            for snapshot in db.scalars(select(SourceSnapshot)):
                assert hashlib.sha256(read_object(storage, snapshot.object_ref)).hexdigest() == snapshot.content_hash
                verified += 1
            for repo in db.scalars(select(Repository)):
                generation = build_and_activate(db, settings, repo.id)
                hits, trace = retrieve(db, settings, repo_id=repo.id, query="token ttok")
                rows.append({"repo_id": str(repo.id), "chunks": generation.chunk_count, "retrieval_hits": len(hits), "degraded": trace.degraded})
        print(json.dumps({"database": args.database, "snapshots_verified": verified, "rebuilt_indexes": rows, "model_requests": 0}))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
