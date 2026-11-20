"""Offline backup and isolated restore rehearsal; never overwrite an existing DB.

Stop API/Worker before backup. Uses pg_dump/pg_restore inside this project's db
container; credentials stay in its environment. .env/admin credentials and
workspace clones are intentionally excluded. Rebuild derived RAG indexes.
"""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.core.config import get_settings
from sqlalchemy.engine import make_url


def command(*args, **kwargs):
    return subprocess.run(["docker", "compose", "--env-file", str(ROOT / ".env"), "-f", str(ROOT / "infra/compose.yaml"), "exec", "-T", "db", *args], check=True, **kwargs)


def verify(folder):
    folder = folder.resolve()
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in manifest["files"].items():
        file = (folder / name).resolve()
        if not file.is_relative_to(folder) or not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != digest:
            raise RuntimeError("Backup verification failed: " + name)
    return manifest


def backup(destination):
    if (ROOT / "var/local-processes.json").exists():
        raise RuntimeError("Stop local API/Worker first using scripts/stop_local.ps1")
    settings = get_settings()
    url = make_url(settings.database_url.get_secret_value())
    folder = destination.resolve()
    folder.mkdir(parents=True, exist_ok=False)
    with (folder / "database.dump").open("wb") as output:
        command("pg_dump", "-U", url.username, "-d", url.database, "--format=custom", "--no-owner", stdout=output)
    source = settings.storage_root / "objects"
    if source.exists():
        shutil.copytree(source, folder / "objects")
    files = {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob("*") if p.is_file()}
    manifest = {"version": 1, "created_at": datetime.now(UTC).isoformat(), "database": url.database, "files": files, "derived_indexes": "rebuild", "secrets_included": False}
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    verify(folder)
    print(json.dumps({"backup": str(folder), "files": len(files), "verified": True}))


def restore(folder, database, destination):
    manifest = verify(folder)
    if not re.fullmatch(r"devflow_restore_[a-z0-9_]{1,40}", database):
        raise RuntimeError("Restore database must have a devflow_restore_ prefix")
    if destination.exists():
        raise RuntimeError("Restore object destination must be a new directory")
    url = make_url(get_settings().database_url.get_secret_value())
    # createdb fails safely if a database already exists. Never DROP or clean it.
    command("createdb", "-U", url.username, database)
    with (folder / "database.dump").open("rb") as source:
        command("pg_restore", "-U", url.username, "-d", database, "--no-owner", "--exit-on-error", stdin=source)
    destination.mkdir(parents=True, exist_ok=False)
    if (folder / "objects").exists():
        shutil.copytree(folder / "objects", destination / "objects")
    result = command("psql", "-U", url.username, "-d", database, "-At", "-c", "SELECT version_num FROM alembic_version; SELECT count(*) FROM repositories; SELECT count(*) FROM runs; SELECT count(*) FROM evidence;", capture_output=True, text=True)
    print(json.dumps({"restored_database": database, "storage": str(destination.resolve()), "schema_repo_run_evidence_counts": result.stdout.splitlines(), "verified_backup_files": len(manifest["files"])}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["backup", "verify", "restore"])
    parser.add_argument("folder", type=Path)
    parser.add_argument("--database")
    parser.add_argument("--storage", type=Path)
    args = parser.parse_args()
    if args.mode == "backup":
        backup(args.folder)
    elif args.mode == "verify":
        print(json.dumps({"verified_files": len(verify(args.folder)["files"])}))
    elif not args.database or not args.storage:
        parser.error("restore requires --database and --storage")
    else:
        restore(args.folder, args.database, args.storage)
