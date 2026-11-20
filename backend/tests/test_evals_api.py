ORIGIN = {"Origin": "http://localhost:3100"}


def test_latest_evals_requires_login(client):
    assert client.get("/api/v1/evals/latest", headers=ORIGIN).status_code == 401


def test_latest_evals_returns_offline_and_real(logged_in):
    data = logged_in.get("/api/v1/evals/latest", headers=ORIGIN).json()["data"]
    offline = data["offline"]
    assert offline["passed"] == offline["total"]
    assert offline["model_requests"] == 0
    assert offline["model_quality_evaluated"] is False
    assert offline["groups"]
    assert data["real"]["human_review_status"] == "pending"
    assert data["real"]["generalization_claim"] is False
