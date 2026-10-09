from sqlalchemy import func, select

from app.persistence.models import Document, IndexOutbox, Job
from tests.knowledge_helpers import make_repo

ORIGIN = {"Origin": "http://localhost:3100"}

DOC = "# 指南\n\n这是一份测试文档，包含认证与部署说明。\n"


def upload(logged_in, repo_id, filename="guide.md", content: bytes = DOC.encode()):
    return logged_in.post(
        f"/api/v1/repos/{repo_id}/documents",
        files={"file": (filename, content, "text/markdown")},
        headers=ORIGIN,
    )


def test_upload_list_detail_delete_flow(logged_in, database, settings):
    repo_id = make_repo(database)

    response = upload(logged_in, repo_id)
    assert response.status_code == 202
    data = response.json()["data"]
    assert data["changed"] is True
    assert data["document"]["state"] == "active"
    document_id = data["document"]["id"]
    assert data["index_job_id"]

    with database() as db:
        assert db.scalar(select(func.count()).select_from(Document)) == 1
        assert db.scalar(select(func.count()).select_from(IndexOutbox)) == 1
        job = db.scalar(select(Job))
        assert job.kind == "index" and job.entity_id == repo_id

    duplicate = upload(logged_in, repo_id)
    assert duplicate.status_code == 202
    assert duplicate.json()["data"]["changed"] is False

    listing = logged_in.get(f"/api/v1/repos/{repo_id}/documents", headers=ORIGIN).json()["data"]
    assert [item["id"] for item in listing["items"]] == [document_id]

    detail = logged_in.get(f"/api/v1/documents/{document_id}", headers=ORIGIN).json()["data"]
    assert detail["document"]["id"] == document_id
    assert len(detail["versions"]) == 1
    assert detail["current_chunk_count"] >= 1

    conflict = logged_in.request(
        "DELETE", f"/api/v1/documents/{document_id}",
        json={"expected_version": 99}, headers=ORIGIN,
    )
    assert conflict.status_code == 409

    delete = logged_in.request(
        "DELETE", f"/api/v1/documents/{document_id}",
        json={"expected_version": detail["document"]["version"]}, headers=ORIGIN,
    )
    assert delete.status_code == 202
    assert delete.json()["data"]["tombstone"] is True
    assert delete.json()["data"]["document"]["state"] == "deleted"

    # Re-upload resurrects the document as a new version change.
    revived = upload(logged_in, repo_id)
    assert revived.json()["data"]["changed"] is True
    assert revived.json()["data"]["document"]["state"] == "active"


def test_upload_validation(logged_in, database, settings):
    repo_id = make_repo(database)
    assert upload(logged_in, repo_id, filename="data.pdf").status_code == 415
    assert upload(logged_in, repo_id, content=b"\x00\x01binary").status_code == 415
    assert upload(logged_in, repo_id, content=b"   \n").status_code == 400
    settings.max_document_bytes = 1024
    assert upload(logged_in, repo_id, content=b"x" * 2048).status_code == 413


def test_document_scope_and_login_required(client, logged_in, database, settings):
    import uuid

    from fastapi.testclient import TestClient

    from app.main import create_app

    repo_id = make_repo(database)
    document_id = upload(logged_in, repo_id).json()["data"]["document"]["id"]

    with TestClient(create_app(settings, session_factory=database)) as fresh:
        assert fresh.get(f"/api/v1/documents/{document_id}").status_code == 401

    missing = logged_in.get(f"/api/v1/documents/{uuid.uuid4()}", headers=ORIGIN)
    assert missing.status_code == 404
