"""Optional rerank adapter for hybrid retrieval.

The endpoint follows the common Jina/Cohere shape:
``POST {base}/rerank`` with ``{model, query, documents, top_n}`` and a response
of ``{"results": [{"index": int, "relevance_score": float}, ...]}``.
Reranking is a quality upgrade, never a hard dependency: failures degrade to the
RRF order and are recorded in the retrieval trace.
"""

import logging

import httpx

from app.core.config import Settings

log = logging.getLogger("devflow.rerank")


class RerankUnavailable(Exception):
    """Rerank service missing, unreachable, or returned an unusable payload."""


class RerankClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        if not settings.rerank_configured:
            raise RerankUnavailable("未配置重排服务")
        self.base = settings.rerank_base_url.rstrip("/")
        self.model = settings.rerank_model
        self.key = settings.rerank_api_key.get_secret_value()
        self.timeout = httpx.Timeout(settings.rerank_timeout_seconds, connect=10, write=10)
        self.transport = transport

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        if not documents:
            return []
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        payload = {
            "model": self.model,
            "query": query,
            "documents": documents,
            "top_n": min(top_n, len(documents)),
        }
        try:
            with httpx.Client(
                timeout=self.timeout, transport=self.transport, follow_redirects=False
            ) as client:
                response = client.post(self.base + "/rerank", json=payload, headers=headers)
        except httpx.HTTPError:
            raise RerankUnavailable("无法连接重排服务") from None
        if response.status_code in {401, 403}:
            raise RerankUnavailable("重排服务拒绝访问")
        if response.status_code == 429:
            raise RerankUnavailable("重排服务限流")
        if response.status_code >= 400:
            raise RerankUnavailable(f"重排服务返回 {response.status_code}")
        try:
            body = response.json()
            items = body.get("results") or body.get("data") or []
            pairs = [
                (int(item["index"]), float(item.get("relevance_score", item.get("score"))))
                for item in items
            ]
        except (AttributeError, KeyError, TypeError, ValueError):
            raise RerankUnavailable("重排响应格式不正确") from None
        if not pairs:
            raise RerankUnavailable("重排没有返回结果")
        return pairs
