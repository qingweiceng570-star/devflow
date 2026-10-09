import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from app.core.config import Settings
from app.core.errors import DomainError

log = logging.getLogger("devflow.github")

MAX_RESPONSE_BYTES = 5 * 1024 * 1024
RETRY_DELAYS = (0.5, 1.5)


class SourceRateLimited(DomainError):
    """Upstream quota exhausted; the caller must schedule a later retry."""

    def __init__(self, retry_after: int):
        super().__init__(
            "SOURCE_RATE_LIMITED", "GitHub 接口额度已用尽，稍后自动重试", 429, retryable=True
        )
        self.retry_after = max(1, retry_after)


@dataclass
class GitHubPage:
    data: object
    etag: str | None
    next_cursor: str | None
    not_modified: bool = False


def parse_next_cursor(link_header: str | None, base: str) -> str | None:
    """Extract the rel="next" target as an origin-locked relative cursor."""
    if not link_header:
        return None
    for part in link_header.split(","):
        segments = part.split(";")
        if len(segments) < 2:
            continue
        url = segments[0].strip()
        if not url.startswith("<") or not url.endswith(">"):
            continue
        rel_next = any(
            seg.strip() in {'rel="next"', "rel=next"} for seg in segments[1:]
        )
        if not rel_next:
            continue
        url = url[1:-1]
        parsed = urlsplit(url)
        if parsed.netloc and parsed.netloc != urlsplit(base).netloc:
            raise DomainError("SOURCE_UNAVAILABLE", "远端返回了越界的翻页地址", 502)
        if parsed.scheme and parsed.scheme != urlsplit(base).scheme:
            raise DomainError("SOURCE_UNAVAILABLE", "远端返回了越界的翻页地址", 502)
        cursor = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        if not cursor.startswith("/") or "://" in cursor:
            raise DomainError("SOURCE_UNAVAILABLE", "远端返回了越界的翻页地址", 502)
        return cursor
    return None


def valid_cursor(cursor: str) -> str:
    """Re-validate a stored cursor before it is sent back to the API."""
    parsed = urlsplit(cursor)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        raise DomainError("SOURCE_UNAVAILABLE", "保存的翻页游标不合法", 502)
    return cursor


def rate_limit_wait(status: int, headers: httpx.Headers, body: object) -> int | None:
    """Return seconds to wait when the response is a quota rejection."""
    if status == 429:
        retry_after = headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            return max(1, int(retry_after))
        return 60
    if status == 403:
        remaining = headers.get("x-ratelimit-remaining")
        if remaining == "0":
            reset = headers.get("x-ratelimit-reset")
            if reset and reset.isdigit():
                wait = int(reset) - int(datetime.now(UTC).timestamp())
                return max(1, wait)
            return 60
        message = ""
        if isinstance(body, dict):
            message = str(body.get("message", ""))
        if "rate limit" in message.lower() or "abuse" in message.lower():
            retry_after = headers.get("retry-after")
            if retry_after and retry_after.isdigit():
                return max(1, int(retry_after))
            return 60
    return None


class GitHubClient:
    """Bounded GitHub REST reader: origin-locked pagination, conditional requests,
    retries for transient failures, and quota-aware error mapping."""

    def __init__(self, settings: Settings, token: str, transport: httpx.BaseTransport | None = None):
        self.base = settings.github_api_base
        self.token = token
        self.transport = transport
        self.trust_env = settings.github_trust_env
        self.timeout = httpx.Timeout(
            settings.http_read_timeout_seconds, connect=10, write=10, pool=10
        )
        self.max_retries = settings.http_max_retries

    def get(self, path: str, params: dict | None = None, etag: str | None = None) -> GitHubPage:
        url = self.base + valid_cursor(path)
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "devflow-ai-m1",
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if etag:
            headers["If-None-Match"] = etag
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                time.sleep(RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)])
            try:
                with httpx.Client(
                    timeout=self.timeout,
                    transport=self.transport,
                    follow_redirects=False,
                    trust_env=self.trust_env,
                ) as client:
                    response = client.get(url, params=params, headers=headers)
            except httpx.HTTPError as error:
                last_error = error
                log.warning("github transport error attempt=%s type=%s", attempt, type(error).__name__)
                continue
            if response.status_code == 304:
                return GitHubPage(data=None, etag=etag, next_cursor=None, not_modified=True)
            if response.status_code == 200:
                if len(response.content) > MAX_RESPONSE_BYTES:
                    raise DomainError("SOURCE_UNAVAILABLE", "GitHub 响应超过处理上限", 502)
                try:
                    data = response.json()
                except ValueError:
                    raise DomainError("SOURCE_UNAVAILABLE", "GitHub 返回了非法 JSON", 502) from None
                return GitHubPage(
                    data=data,
                    etag=response.headers.get("etag"),
                    next_cursor=parse_next_cursor(response.headers.get("link"), self.base),
                )
            if response.status_code >= 500:
                last_error = DomainError("SOURCE_UNAVAILABLE", "GitHub 暂时不可用", 502, retryable=True)
                continue
            wait = rate_limit_wait(response.status_code, response.headers, self._safe_json(response))
            if wait is not None:
                raise SourceRateLimited(wait)
            if response.status_code in {401, 403, 404}:
                log.info("github access rejected status=%s", response.status_code)
                raise DomainError(
                    "SOURCE_UNAVAILABLE", "无法读取远端资源：不存在或无权访问", 502
                )
            raise DomainError("SOURCE_UNAVAILABLE", "GitHub 拒绝了请求", 502)
        if isinstance(last_error, DomainError):
            raise last_error
        raise DomainError("SOURCE_UNAVAILABLE", "无法连接 GitHub", 502, retryable=True)

    @staticmethod
    def _safe_json(response: httpx.Response) -> object:
        try:
            return response.json()
        except ValueError:
            return None
