import type { NextRequest } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  if (path.some((segment) => !/^[A-Za-z0-9_-]+$/.test(segment))) {
    return Response.json({ error: { code: "INVALID_PATH", message: "接口路径不合法", retryable: false } }, { status: 400 });
  }
  const base = process.env.DEVFLOW_API_URL || "http://127.0.0.1:8100";
  const url = `${base}/api/v1/${path.join("/")}${request.nextUrl.search}`;
  const headers = new Headers();
  for (const name of ["cookie", "origin", "content-type", "x-csrf-token", "idempotency-key", "last-event-id"]) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  try {
    const body = request.method === "GET" || request.method === "HEAD" ? undefined : await request.arrayBuffer();
    if (body && body.byteLength > 1024 * 1024) {
      return Response.json({ error: { code: "PAYLOAD_TOO_LARGE", message: "M0 请求不能超过 1 MiB", retryable: false } }, { status: 413 });
    }
    const upstream = await fetch(url, {
      method: request.method, headers, body, cache: "no-store", redirect: "manual",
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(65000)]),
    });
    const responseHeaders = new Headers({ "Cache-Control": "no-store" });
    for (const name of ["content-type", "x-request-id", "x-content-type-options"]) {
      const value = upstream.headers.get(name);
      if (value) responseHeaders.set(name, value);
    }
    for (const cookie of upstream.headers.getSetCookie()) responseHeaders.append("set-cookie", cookie);
    // M0 exposes bounded JSON diagnostics. Buffering also ensures an upstream
    // completed response is closed before returning it to the browser.
    const responseBody = upstream.status === 204 ? null : await upstream.arrayBuffer();
    return new Response(responseBody, { status: upstream.status, headers: responseHeaders });
  } catch {
    return Response.json({ error: { code: "API_UNAVAILABLE", message: "后端连接不可用或请求超时", retryable: true } }, { status: 503 });
  }
}

export const GET = proxy;
export const POST = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
