export class ApiError extends Error {
  constructor(public code: string, message: string, public status: number) { super(message); }
}

export function csrfToken(): string {
  const value = document.cookie.split("; ").find((part) => part.startsWith("devflow_csrf="))?.split("=").slice(1).join("=") || "";
  try { return decodeURIComponent(value); } catch { return value; }
}

type ApiEnvelope<T> = { data?: T; error?: { code?: unknown; message?: unknown } };

const DEFAULT_TIMEOUT_MS = 15_000;
const LONG_REQUEST_TIMEOUT_MS = 65_000;

function timeoutFor(path: string, upload = false): number {
  // Repository import may fall back to a git check when the GitHub API quota
  // is exhausted, so it can legitimately take longer than a normal request.
  if (path === "/repos") return LONG_REQUEST_TIMEOUT_MS;
  if (upload || path.endsWith("/probe") || path.includes("/ci-options") || path.includes("/github-diagnostics")) {
    return LONG_REQUEST_TIMEOUT_MS;
  }
  return DEFAULT_TIMEOUT_MS;
}

function isTimeout(error: unknown): boolean {
  return error instanceof DOMException && (error.name === "TimeoutError" || error.name === "AbortError");
}

async function request<T>(path: string, init: RequestInit, timeoutMs: number, networkMessage: string): Promise<T> {
  try {
    const timeout = AbortSignal.timeout(timeoutMs);
    const signal = init.signal ? AbortSignal.any([init.signal, timeout]) : timeout;
    const response = await fetch(`/api/v1${path}`, { ...init, signal });
    if (response.status === 204) return undefined as T;

    let payload: ApiEnvelope<T>;
    try { payload = await response.json() as ApiEnvelope<T>; }
    catch { throw new ApiError("INVALID_RESPONSE", "服务返回格式不正确，请检查后端连接", response.status); }

    if (!response.ok) {
      const error = payload && typeof payload.error === "object" && payload.error ? payload.error : {};
      throw new ApiError(
        typeof error.code === "string" ? error.code : "REQUEST_FAILED",
        typeof error.message === "string" ? error.message : "请求未完成",
        response.status,
      );
    }
    return payload.data as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (isTimeout(error)) throw new ApiError("TIMEOUT", "请求超时，请稍后重试", 0);
    throw new ApiError("NETWORK_ERROR", networkMessage, 0);
  }
}

export async function api<T>(path: string, body?: unknown, method?: string, signal?: AbortSignal): Promise<T> {
  const hasBody = body !== undefined;
  return request<T>(path, {
    method: method || (hasBody ? "POST" : "GET"),
    signal,
    credentials: "same-origin",
    headers: hasBody ? { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() } : {},
    body: hasBody ? JSON.stringify(body) : undefined,
  }, timeoutFor(path), "无法连接服务，请检查后端是否已启动");
}

export async function apiUpload<T>(path: string, file: File, fields?: Record<string, string>): Promise<T> {
  const form = new FormData();
  form.append("file", file);
  for (const [key, value] of Object.entries(fields || {})) form.append(key, value);
  return request<T>(path, {
    method: "POST", credentials: "same-origin", body: form,
    headers: { "X-CSRF-Token": csrfToken() },
  }, timeoutFor(path, true), "上传失败：无法连接服务");
}
