"""Bounded Streamable HTTP MCP client (JSON and SSE responses)."""

import hashlib
import json
import time
from urllib.parse import urlsplit

import httpx
from jsonschema import Draft202012Validator

from app.core.errors import DomainError


def schema_hash(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def check_schema(schema):
    Draft202012Validator.check_schema(schema)

    # Do not resolve remote references supplied by a server/template.
    def walk(value):
        if isinstance(value, dict):
            for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
                if keyword in value and not value[keyword].startswith("#"):
                    raise DomainError("INVALID_SCHEMA", "禁止外部 Schema 引用", 400)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(schema)


class MCPClient:
    def __init__(self, settings, endpoint, *, transport=None):
        parsed = urlsplit(endpoint)
        if (
            endpoint not in settings.mcp_server_allowlist
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.scheme not in {"https", "http"}
            or (
                parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            )
        ):
            raise DomainError("MCP_NOT_ALLOWED", "MCP 地址未获服务端白名单授权", 403)
        self.endpoint, self.sequence, self.session = endpoint, 0, None
        self.client = httpx.Client(
            timeout=30, trust_env=False, follow_redirects=False, transport=transport
        )
        self.headers = {
            "Accept": "application/json, text/event-stream",
            "Origin": settings.app_origin,
        }

    def close(self):
        self.client.close()

    def rpc(self, method, params=None, notification=False):
        self.sequence += 1
        body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notification:
            body["id"] = self.sequence
        headers = dict(self.headers)
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        started = time.monotonic()
        try:
            with self.client.stream("POST", self.endpoint, json=body, headers=headers) as response:
                if response.status_code not in {200, 202}:
                    raise DomainError("MCP_UNAVAILABLE", "MCP 服务拒绝请求", 502)
                if method == "initialize":
                    self.session = response.headers.get("Mcp-Session-Id")
                if notification:
                    return {}
                data = bytearray()
                sse = "text/event-stream" in response.headers.get("content-type", "")
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 65536 or time.monotonic() - started > 30:
                        raise DomainError("MCP_RESPONSE_LIMIT", "MCP 结果超过 64 KiB 或 30 秒", 502)
                    if sse:
                        normalized = bytes(data).replace(b"\r\n", b"\n")
                        for event in normalized.split(b"\n\n")[:-1]:
                            payload = b"\n".join(
                                line[5:].lstrip()
                                for line in event.split(b"\n")
                                if line.startswith(b"data:")
                            )
                            if payload:
                                result = json.loads(payload)
                                if not isinstance(result, dict):
                                    raise DomainError("MCP_PROTOCOL", "MCP 响应必须是对象", 502)
                                if result.get("id") == self.sequence:
                                    return self.result(result)
                if sse:
                    raise DomainError("MCP_PROTOCOL", "MCP 流缺少对应响应", 502)
                result = json.loads(data)
                if not isinstance(result, dict):
                    raise DomainError("MCP_PROTOCOL", "MCP 响应必须是对象", 502)
                if result.get("id") != self.sequence:
                    raise DomainError("MCP_PROTOCOL", "MCP 响应 ID 不匹配", 502)
                return self.result(result)
        except (httpx.HTTPError, ValueError, TypeError):
            raise DomainError("MCP_UNAVAILABLE", "MCP 连接或响应不可用", 502) from None

    @staticmethod
    def result(value):
        if value.get("error") or not isinstance(value.get("result"), dict):
            raise DomainError("MCP_PROTOCOL", "MCP 返回协议错误", 502)
        return value["result"]

    def initialize(self):
        result = self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "devflow", "version": "1.0"},
            },
        )
        if result.get("protocolVersion") not in {"2025-03-26", "2025-06-18"}:
            raise DomainError("MCP_PROTOCOL", "不支持该 MCP 协议版本", 502)
        self.headers["MCP-Protocol-Version"] = result["protocolVersion"]
        self.rpc("notifications/initialized", notification=True)

    def tools(self):
        tools, cursor = [], None
        for _ in range(5):
            page = self.rpc("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(page.get("tools", []))
            cursor = page.get("nextCursor")
            if not cursor:
                return tools
        raise DomainError("MCP_RESPONSE_LIMIT", "MCP 工具清单超过分页上限", 502)
