from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.persistence.models import Session, User
from tests.conftest import PASSWORD


def test_protected_route_requires_login(client):
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_login_cookie_and_server_hash(logged_in, database):
    assert logged_in.get("/api/v1/auth/me").json()["data"]["username"] == "admin"
    with database() as db:
        row = db.scalar(select(Session))
        assert row.token_hash != logged_in.cookies.get("devflow_session")
        assert row.csrf_hash != logged_in.cookies.get("devflow_csrf")
        assert PASSWORD not in db.scalar(select(User)).password_hash


def test_session_cookie_is_httponly(client):
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": PASSWORD},
        headers={"Origin": "http://localhost:3100"},
    )
    cookies = response.headers.get_list("set-cookie")
    assert "HttpOnly" in next(c for c in cookies if c.startswith("devflow_session="))
    assert all("SameSite=strict" in c for c in cookies)


def test_invalid_login_does_not_reveal_username(client):
    responses = [
        client.post(
            "/api/v1/auth/login",
            json={"username": name, "password": "bad"},
            headers={"Origin": "http://localhost:3100"},
        )
        for name in ["admin", "missing"]
    ]
    assert all(r.status_code == 401 for r in responses)
    assert responses[0].json()["error"] == responses[1].json()["error"]


def test_cross_origin_login_rejected(client):
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": PASSWORD},
        headers={"Origin": "https://untrusted.example"},
    )
    assert response.status_code == 403


def test_missing_csrf_cannot_probe(logged_in):
    logged_in.headers.pop("X-CSRF-Token")
    assert logged_in.post("/api/v1/settings/capabilities/probe", json={}).status_code == 403


def test_logout_revokes_stolen_cookie(logged_in):
    stolen = logged_in.cookies.get("devflow_session")
    assert logged_in.post("/api/v1/auth/logout").status_code == 204
    assert (
        logged_in.get(
            "/api/v1/auth/me", headers={"Cookie": f"devflow_session={stolen}"}
        ).status_code
        == 401
    )


def test_expired_session_rejected(logged_in, database):
    with database() as db:
        db.scalar(select(Session)).expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    assert logged_in.get("/api/v1/auth/me").status_code == 401


def test_disabled_user_rejected(logged_in, database):
    with database() as db:
        db.scalar(select(User)).disabled = True
        db.commit()
    assert logged_in.get("/api/v1/auth/me").status_code == 401


def test_validation_errors_do_not_echo_secrets(client):
    secret = "private-password-should-not-appear"
    response = client.post(
        "/api/v1/auth/login", json={"password": secret}, headers={"Origin": "http://localhost:3100"}
    )
    assert response.status_code == 422
    assert secret not in response.text


def test_login_limiter(client):
    body = {"username": "missing", "password": "bad"}
    for _ in range(10):
        assert (
            client.post(
                "/api/v1/auth/login", json=body, headers={"Origin": "http://localhost:3100"}
            ).status_code
            == 401
        )
    assert (
        client.post(
            "/api/v1/auth/login", json=body, headers={"Origin": "http://localhost:3100"}
        ).status_code
        == 429
    )
