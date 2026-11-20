import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit

import httpx

from app.core.config import Settings
from app.core.errors import DomainError

log = logging.getLogger("devflow.github")

MAX_RESPONSE_BYTES = 5 * 1024 * 1024
RETRY_DELAYS = (0.5, 1.5)
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 3
NETWORK_HINT = (
    "无法连接 GitHub，请检查网络；需要代理时在 .env 设置 "
    "GITHUB_TRUST_ENV=true 或 GITHUB_API_PROXY 后重启后端"
)


def access_error(status: int, *, log_download=False, signed_url=False) -> DomainError:
    if signed_url and status in {401, 403, 404}:
        return DomainError(
            "LOG_URL_EXPIRED",
            "日志临时下载地址不可用，请重新获取日志链接",
            502,
            retryable=True,
            details={"upstream_status": status},
        )
    code, message, remedy = {
        401: (
            "GITHUB_AUTH_INVALID",
            "GitHub 凭据无效或已过期",
            "在本地配置有效的读取令牌并重启服务",
        ),
        403: (
            "GITHUB_PERMISSION_DENIED",
            "GitHub 拒绝读取此资源",
            "确认令牌包含目标仓库及 Actions: read；组织仓库还需确认 SSO 授权",
        ),
        404: (
            "GITHUB_RESOURCE_UNAVAILABLE",
            "资源不存在或当前凭据不可见",
            "核对仓库、run/attempt/job；404 无法单独证明日志过期",
        ),
        410: (
            "GITHUB_LOG_EXPIRED",
            "GitHub 日志资源已不可用",
            "检查日志保留期，选择仍保留日志的运行",
        ),
    }.get(
        status,
        (
            "LOG_UNAVAILABLE" if log_download else "SOURCE_UNAVAILABLE",
            "GitHub 资源暂不可用",
            "稍后重试或查看 GitHub 服务状态",
        ),
    )
    return DomainError(code, message, 502, details={"upstream_status": status, "remedy": remedy})


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
        rel_next = any(seg.strip() in {'rel="next"', "rel=next"} for seg in segments[1:])
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

    def __init__(
        self, settings: Settings, token: str, transport: httpx.BaseTransport | None = None
    ):
        self.base = settings.github_api_base
        self.token = token
        self.transport = transport
        self.trust_env = settings.github_trust_env
        # An explicit proxy wins over GITHUB_TRUST_ENV; both are only used to
        # reach the configured GitHub API origin.
        self.proxy = settings.github_api_proxy or None
        self.timeout = httpx.Timeout(
            settings.http_read_timeout_seconds, connect=10, write=10, pool=10
        )
        self.max_retries = settings.http_max_retries

    def _same_origin(self, url: str) -> bool:
        base = urlsplit(self.base)
        target = urlsplit(url)
        if target.scheme != base.scheme:
            return False
        if (target.hostname or "").lower() != (base.hostname or "").lower():
            return False
        try:
            base_port = base.port or (443 if base.scheme == "https" else 80)
            target_port = target.port or (443 if target.scheme == "https" else 80)
        except ValueError:
            return False
        return base_port == target_port

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
                    proxy=self.proxy,
                ) as client:
                    request_url, request_params = url, params
                    for _hop in range(MAX_REDIRECTS + 1):
                        response = client.get(request_url, params=request_params, headers=headers)
                        if response.status_code not in REDIRECT_STATUSES:
                            break
                        location = response.headers.get("location")
                        if not location:
                            break
                        target = urljoin(request_url, location)
                        # A renamed repository answers /repos/old/name with a
                        # same-origin redirect; never follow it elsewhere.
                        if not self._same_origin(target):
                            raise DomainError(
                                "SOURCE_UNAVAILABLE", "GitHub 返回了越界的重定向", 502
                            )
                        request_url, request_params = target, None
                    else:
                        raise DomainError(
                            "SOURCE_UNAVAILABLE", "GitHub 重定向次数过多", 502
                        )
            except httpx.HTTPError as error:
                last_error = error
                log.warning(
                    "github transport error attempt=%s type=%s", attempt, type(error).__name__
                )
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
                last_error = DomainError(
                    "SOURCE_UNAVAILABLE", "GitHub 暂时不可用", 502, retryable=True
                )
                continue
            wait = rate_limit_wait(
                response.status_code, response.headers, self._safe_json(response)
            )
            if wait is not None:
                raise SourceRateLimited(wait)
            if response.status_code in {401, 403, 404}:
                log.info("github access rejected status=%s", response.status_code)
                raise access_error(response.status_code)
            raise DomainError("SOURCE_UNAVAILABLE", "GitHub 拒绝了请求", 502)
        if isinstance(last_error, DomainError):
            raise last_error
        raise DomainError("SOURCE_UNAVAILABLE", NETWORK_HINT, 502, retryable=True)

    @staticmethod
    def _safe_json(response: httpx.Response) -> object:
        try:
            return response.json()
        except ValueError:
            return None

    def download_log(self, path: str, max_bytes: int) -> tuple[bytes, bool]:
        """Stream a job's plain-text log. Signed storage redirects never receive credentials."""
        url = self.base + valid_cursor(path)
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        try:
            with httpx.Client(
                timeout=self.timeout,
                transport=self.transport,
                trust_env=self.trust_env,
                proxy=self.proxy,
                follow_redirects=False,
            ) as client:
                for hop in range(3):
                    with client.stream("GET", url, headers=headers) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            target = response.headers.get("location", "")
                            parsed = urlsplit(target)
                            host = parsed.hostname or ""
                            allowed = host.endswith(".blob.core.windows.net") or host.endswith(
                                ".actions.githubusercontent.com"
                            )
                            if (
                                not allowed
                                or parsed.scheme != "https"
                                or parsed.username
                                or parsed.password
                                or parsed.port not in {None, 443}
                                or hop == 2
                            ):
                                raise DomainError("LOG_UNAVAILABLE", "日志下载地址未获允许", 502)
                            url, headers = target, {}
                            continue
                        wait = rate_limit_wait(response.status_code, response.headers, {})
                        if wait is not None:
                            raise SourceRateLimited(wait)
                        if response.status_code != 200:
                            raise access_error(
                                response.status_code, log_download=True, signed_url=hop > 0
                            )
                        data = bytearray()
                        for chunk in response.iter_bytes(chunk_size=16384):
                            remaining = max_bytes - len(data)
                            data.extend(chunk[:remaining])
                            if len(chunk) > remaining:
                                return bytes(data), True
                        return bytes(data), False
        except httpx.HTTPError:
            raise DomainError("LOG_UNAVAILABLE", "日志下载失败", 502, retryable=True) from None
        raise DomainError("LOG_UNAVAILABLE", "日志跳转超过上限", 502)
