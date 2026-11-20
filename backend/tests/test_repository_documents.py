import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.core.errors import DomainError
from app.ingestion import repository_documents
from app.ingestion.repository_documents import is_document_path, read_document_snapshot
from app.jobs.handlers.import_documents import handle_import_documents
from app.jobs.queue import LeaseLost, claim_next, complete
from app.jobs.recovery import recover_expired
from app.persistence.models import Document, DocumentVersion, Job, Repository
from app.rag.retriever import retrieve
from app.services import knowledge_service
from app.workspace.manager import WorkspaceManager
from tests.knowledge_helpers import make_repo, run_index_job, upload

ORIGIN = {"Origin": "http://localhost:3100"}
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def source(tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    git(folder, "init", "-q", "-b", "main")
    git(folder, "config", "user.email", "test@example.test")
    git(folder, "config", "user.name", "Test")
    files = {
        "README.md": b"# Quasar\n\nQuasar processes invoices.\n",
        "docs/design.mdx": "# 权限设计\n\n写操作需要 maintainer 权限。\n".encode(),
        "docs/empty.md": b"   \n",
        "docs/binary.md": b"\x00binary",
        ".env.txt": b"should never be imported",
        "node_modules/docs/vendor.md": b"third party",
        "src/app.py": b"print('not a document')",
    }
    for name, data in files.items():
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    git(folder, "add", "-A")
    git(folder, "commit", "-qm", "initial")
    return folder


def execute_import(database, settings, repo_id, source):
    with database() as db:
        knowledge_service.request_repository_import(db, db.get(Repository, repo_id))
        job = claim_next(db, worker_id="docs-worker", lease_seconds=60)
        db.commit()
        assert job.kind == "import_docs"
    result = handle_import_documents(
        database, settings, job, "docs-worker",
        workspace_factory=lambda s, t: WorkspaceManager(s, remote_url=lambda _: str(source)),
    )
    with database() as db:
        assert complete(db, job=db.get(Job, job.id), epoch=job.lease_epoch)
        db.commit()
    return result


def test_repository_import_builds_searchable_knowledge(database, settings, source):
    repo_id = make_repo(database)
    result = execute_import(database, settings, repo_id, source)
    assert result["imported"] == 2
    assert result["skipped_count"] == 2
    run_index_job(database, settings, repo_id)
    with database() as db:
        documents = db.scalars(select(Document)).all()
        assert {d.source_key for d in documents} == {"git:README.md", "git:docs/design.mdx"}
        assert all(d.source_revision == result["commit_sha"] for d in documents)
        hits, _ = retrieve(db, settings, repo_id=repo_id, query="maintainer 权限")
        assert hits and hits[0].source_key == "git:docs/design.mdx"
        status = knowledge_service.index_status(db, repo_id)
        assert status["document_count"] == 2
        assert status["document_sources"] == {"repository": 2, "uploaded": 0, "generated": 0}
        assert status["import_job"]["result"]["imported"] == 2


def test_import_reuses_content_and_reconciles_deleted_paths(database, settings, source):
    repo_id = make_repo(database)
    first = execute_import(database, settings, repo_id, source)
    run_index_job(database, settings, repo_id)
    repeated = execute_import(database, settings, repo_id, source)
    assert repeated["imported"] == 0 and repeated["unchanged"] == 2
    with database() as db:
        assert db.scalar(select(func.count()).select_from(DocumentVersion)) == 2
    upload(database, settings, repo_id, "README.md", b"# Personal notes\nManual upload stays.\n")
    run_index_job(database, settings, repo_id)
    (source / "README.md").unlink()
    (source / "docs/design.mdx").write_text("# 权限设计\n\n部署需要 operator 权限。\n", encoding="utf-8")
    git(source, "add", "-A")
    git(source, "commit", "-qm", "update documentation")
    updated = execute_import(database, settings, repo_id, source)
    assert updated["commit_sha"] != first["commit_sha"]
    assert updated["imported"] == 1 and updated["removed"] == 1
    run_index_job(database, settings, repo_id)
    with database() as db:
        by_source = {d.source_key: d for d in db.scalars(select(Document))}
        assert by_source["git:README.md"].state == "deleted"
        assert by_source["upload:README.md"].state == "active"
        assert by_source["git:docs/design.mdx"].version == 2
        assert not retrieve(db, settings, repo_id=repo_id, query="maintainer")[0]
        assert retrieve(db, settings, repo_id=repo_id, query="operator")[0]


def test_import_limits_and_symlinks_are_explicit(database, settings, source, monkeypatch):
    repo_id = make_repo(database)
    git(source, "update-index", "--add", "--cacheinfo", "120000,"
        + git(source, "hash-object", "README.md").stdout.strip() + ",docs/link.md")
    git(source, "commit", "-qm", "add link")
    monkeypatch.setattr(repository_documents, "MAX_DOCUMENTS", 1)
    with database() as db:
        repo = db.get(Repository, repo_id)
    snapshot = read_document_snapshot(
        settings, WorkspaceManager(settings, remote_url=lambda _: str(source)), repo, check_stop=lambda: None,
    )
    assert [d.path for d in snapshot.documents] == ["README.md"]
    assert any(s["path"] == "docs/link.md" and s["reason"] == "非普通文件" for s in snapshot.skipped)
    assert any("上限" in s["reason"] for s in snapshot.skipped)


def test_import_endpoint_dedupes_and_enforces_scope(logged_in, database):
    repo_id = make_repo(database)
    path = f"/api/v1/repos/{repo_id}/knowledge/import"
    first = logged_in.post(path, json={}, headers=ORIGIN)
    second = logged_in.post(path, json={}, headers=ORIGIN)
    assert first.status_code == second.status_code == 202
    assert first.json()["data"]["job"]["id"] == second.json()["data"]["job"]["id"]
    assert logged_in.post(path, json={}, headers={**ORIGIN, "X-CSRF-Token": "wrong"}).status_code == 403
    assert logged_in.post(f"/api/v1/repos/{uuid4()}/knowledge/import", json={}, headers=ORIGIN).status_code == 404
    with database() as db:
        repo = db.get(Repository, repo_id)
        repo.state = "disabled"
        db.commit()
    assert logged_in.post(path, json={}, headers=ORIGIN).status_code == 409


def test_stale_worker_cannot_publish_documents(database, settings, source):
    repo_id = make_repo(database)
    with database() as db:
        knowledge_service.request_repository_import(db, db.get(Repository, repo_id))
        job = claim_next(db, worker_id="old", lease_seconds=60)
        db.commit()
    class InterruptedManager(WorkspaceManager):
        def ensure_mirror(self, repo):
            result = super().ensure_mirror(repo)
            with database() as db:
                row = db.get(Job, job.id)
                row.lease_owner = "new"
                row.lease_epoch += 1
                db.commit()
            return result

    with pytest.raises(LeaseLost):
        handle_import_documents(database, settings, job, "old", workspace_factory=lambda s, t: InterruptedManager(s, remote_url=lambda _: str(source)))
    with database() as db:
        assert db.scalar(select(func.count()).select_from(Document)) == 0


def test_import_clone_failure_and_recovery(database, settings, source):
    repo_id = make_repo(database)
    with database() as db:
        repo = db.get(Repository, repo_id)
        knowledge_service.request_repository_import(db, repo)
        job = claim_next(db, worker_id="w1", lease_seconds=60)
        db.commit()
    with pytest.raises(DomainError, match="确认 GitHub 链接") as caught:
        handle_import_documents(database, settings, job, "w1", workspace_factory=lambda s, t: WorkspaceManager(s, remote_url=lambda _: str(source / "missing")))
    assert caught.value.code == "DOCUMENT_SOURCE_UNAVAILABLE"
    with database() as db:
        assert db.scalar(select(func.count()).select_from(Document)) == 0
        db.get(Job, job.id).lease_until = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    recovered = recover_expired(database, settings)
    assert recovered[0]["action"] == "retry_wait"


@pytest.mark.parametrize("path,expected", [
    ("README", True), ("README.rst", True), ("docs/design.mdx", True),
    ("packages/api/docs/usage.md", True), ("src/README.md", True),
    ("src/code.py", False), (".env.txt", False), (".github/secrets.md", False),
    ("node_modules/docs/guide.md", False), ("build/docs/generated.md", False),
])
def test_document_path_scope(path, expected):
    assert is_document_path(path) is expected
