import httpx
import pytest

from app.core.errors import DomainError
from app.integrations.github.client import (
    GitHubClient,
    SourceRateLimited,
    parse_next_cursor,
    valid_cursor,
)


def client(settings, handler, **kwargs) -> GitHubClient:
    settings.github_api_base = "https://api.github.com"
    return GitHubClient(settings, "tok", transport=httpx.MockTransport(handler))


def test_link_header_parsing():
    link = '<https://api.github.com/repos/o/r/issues?page=2>; rel="next", <https://api.github.com/repos/o/r/issues?page=5>; rel="last"'
    assert parse_next_cursor(link, "https://api.github.com") == "/repos/o/r/issues?page=2"
    assert parse_next_cursor(None, "https://api.github.com") is None
    cross_host = '<https://evil.example/repos/o/r/issues?page=2>; rel="next"'
    with pytest.raises(DomainError):
        parse_next_cursor(cross_host, "https://api.github.com")


def test_valid_cursor_rejects_absolute_urls():
    assert valid_cursor("/repos/o/r?page=2") == "/repos/o/r?page=2"
    for bad in ["https://evil.example/x", "//evil.example/x", "relative/path"]:
        with pytest.raises(DomainError):
            valid_cursor(bad)


def test_rate_limit_403_variants(settings):
    def handler(_request):
        return httpx.Response(
            403,
            json={"message": "API rate limit exceeded"},
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"},
        )

    with pytest.raises(SourceRateLimited) as caught:
        client(settings, handler).get("/repos/o/r")
    assert caught.value.retry_after > 0

    def handler429(_request):
        return httpx.Response(429, headers={"retry-after": "42"})

    with pytest.raises(SourceRateLimited) as caught:
        client(settings, handler429).get("/repos/o/r")
    assert caught.value.retry_after == 42


def test_plain_403_is_not_a_rate_limit(settings):
    def handler(_request):
        return httpx.Response(403, json={"message": "Must have admin rights"})

    with pytest.raises(DomainError) as caught:
        client(settings, handler).get("/repos/o/r")
    assert caught.value.code == "SOURCE_UNAVAILABLE"
    assert not caught.value.retryable


def test_404_and_401_are_source_unavailable(settings):
    for status in (401, 404):
        with pytest.raises(DomainError) as caught:
            client(settings, lambda _r, s=status: httpx.Response(s)).get("/repos/o/r")
        assert caught.value.code == "SOURCE_UNAVAILABLE"


def test_retry_on_5xx_then_success(settings, monkeypatch):
    settings.http_max_retries = 2
    monkeypatch.setattr("app.integrations.github.client.time.sleep", lambda _s: None)
    attempts = {"n": 0}

    def handler(_request):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(500, json={"message": "boom"})
        return httpx.Response(200, json={"ok": True})

    page = client(settings, handler).get("/repos/o/r")
    assert page.data == {"ok": True}
    assert attempts["n"] == 3


def test_5xx_exhaustion_is_retryable(settings, monkeypatch):
    settings.http_max_retries = 1
    monkeypatch.setattr("app.integrations.github.client.time.sleep", lambda _s: None)
    with pytest.raises(DomainError) as caught:
        client(settings, lambda _r: httpx.Response(500)).get("/repos/o/r")
    assert caught.value.code == "SOURCE_UNAVAILABLE" and caught.value.retryable


def test_etag_304(settings):
    def handler(request):
        assert request.headers.get("if-none-match") == 'W/"v1"'
        return httpx.Response(304)

    page = client(settings, handler).get("/repos/o/r", etag='W/"v1"')
    assert page.not_modified
    assert page.data is None


def test_auth_header_present(settings):
    def handler(request):
        assert request.headers["authorization"] == "Bearer tok"
        assert request.headers["x-github-api-version"] == "2022-11-28"
        return httpx.Response(200, json={})

    client(settings, handler).get("/repos/o/r")


def test_no_auth_header_when_token_empty(settings):
    def handler(request):
        assert "authorization" not in request.headers
        return httpx.Response(200, json={})

    GitHubClient(settings, "", transport=httpx.MockTransport(handler)).get("/repos/o/r")
