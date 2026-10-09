from tests.knowledge_helpers import make_repo, upload_and_index

ORIGIN = {"Origin": "http://localhost:3100"}

DOC = "# 认证\n\n## 登录\n\n设备码登录流程说明。\n\n## 权限\n\nmaintainer 才能执行写操作。\n"


def test_knowledge_search_and_status(logged_in, database, settings):
    repo_id = make_repo(database)

    empty_status = logged_in.get(
        f"/api/v1/repos/{repo_id}/knowledge/status", headers=ORIGIN
    ).json()["data"]
    assert empty_status["active_generation"] is None

    upload_and_index(database, settings, repo_id, "auth.md", DOC)

    response = logged_in.post(
        f"/api/v1/repos/{repo_id}/knowledge/search",
        json={"query": "maintainer 权限"},
        headers=ORIGIN,
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["results"], data
    top = data["results"][0]
    assert top["document"] == "upload:auth.md"
    assert top["heading_path"] == ["认证", "权限"]
    assert data["trace"]["generation_id"]
    assert data["trace"]["channels"]["bm25"]["count"] >= 1
    assert data["trace"]["degraded"] == []

    status = logged_in.get(
        f"/api/v1/repos/{repo_id}/knowledge/status", headers=ORIGIN
    ).json()["data"]
    assert status["active_generation"]["chunk_count"] >= 1
    assert status["pending_changes"] == 0

    gibberish = logged_in.post(
        f"/api/v1/repos/{repo_id}/knowledge/search",
        json={"query": "zzz-no-such-content"},
        headers=ORIGIN,
    ).json()["data"]
    assert gibberish["results"] == []
    assert gibberish["trace"]["fused_candidates"] == 0


def test_reindex_marks_pending_and_requeues(logged_in, database, settings):
    repo_id = make_repo(database)
    upload_and_index(database, settings, repo_id, "auth.md", DOC)

    response = logged_in.post(
        f"/api/v1/repos/{repo_id}/knowledge/reindex",
        json={"reason": "manual"},
        headers=ORIGIN,
    )
    assert response.status_code == 202
    assert response.json()["data"]["accepted"] is True
    assert response.json()["data"]["pending_changes"] == 1


def test_search_validates_dates(logged_in, database):
    repo_id = make_repo(database)
    bad = logged_in.post(
        f"/api/v1/repos/{repo_id}/knowledge/search",
        json={"query": "x", "date_from": "not-a-date"},
        headers=ORIGIN,
    )
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "INVALID_REQUEST"
