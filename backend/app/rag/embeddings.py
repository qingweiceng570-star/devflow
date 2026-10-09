"""OpenAI-compatible embeddings adapter (README 13.1).

The embedding service is a separate capability from the chat model; it is
only required when the vector channel is enabled. Dimension changes require a
new index generation and a fresh vector collection.
"""

import logging

import httpx

from app.core.config import Settings
from app.core.errors import DomainError

log = logging.getLogger("devflow.embeddings")

MAX_RESPONSE_BYTES = 32 * 1024 * 1024


class EmbeddingClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        if not settings.embedding_configured:
            raise DomainError(
                "EMBEDDING_UNAVAILABLE", "未配置嵌入服务（EMBEDDING_BASE_URL/MODEL）", 503
            )
        self.settings = settings
        self.transport = transport
        self.timeout = httpx.Timeout(settings.embedding_timeout_seconds, connect=10, write=10)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        batch_size = self.settings.embedding_batch_size
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            payload = {"model": self.settings.embedding_model, "input": batch}
            headers = {"Content-Type": "application/json"}
            key = self.settings.embedding_api_key.get_secret_value()
            if key:
                headers["Authorization"] = "Bearer " + key
            url = self.settings.embedding_base_url.rstrip("/") + "/embeddings"
            try:
                with httpx.Client(
                    timeout=self.timeout, transport=self.transport, follow_redirects=False
                ) as client:
                    response = client.post(url, json=payload, headers=headers)
            except httpx.HTTPError:
                raise DomainError(
                    "EMBEDDING_UNAVAILABLE", "无法连接嵌入服务", 503, retryable=True
                ) from None
            if response.status_code in {401, 403}:
                raise DomainError("EMBEDDING_AUTH_FAILED", "嵌入服务拒绝访问", 503)
            if response.status_code == 429:
                raise DomainError("EMBEDDING_RATE_LIMITED", "嵌入服务限流", 429, retryable=True)
            if response.status_code >= 400:
                raise DomainError("EMBEDDING_REQUEST_REJECTED", "嵌入服务拒绝请求", 502)
            if len(response.content) > MAX_RESPONSE_BYTES:
                raise DomainError("EMBEDDING_PROTOCOL_ERROR", "嵌入响应超过上限", 502)
            try:
                body = response.json()
                items = sorted(body["data"], key=lambda item: item.get("index", 0))
                vectors.extend(item["embedding"] for item in items)
            except (KeyError, TypeError, ValueError):
                raise DomainError("EMBEDDING_PROTOCOL_ERROR", "嵌入响应不符合兼容格式", 502) from None
        dimension = self.settings.embedding_dimension
        for vector in vectors:
            if len(vector) != dimension:
                raise DomainError(
                    "EMBEDDING_DIMENSION_MISMATCH",
                    f"嵌入维度 {len(vector)} 与配置 {dimension} 不一致",
                    502,
                )
        return vectors
