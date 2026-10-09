export class ApiError extends Error {
  constructor(public code: string, message: string, public status: number) { super(message); }
}

export function csrfToken(): string {
  return document.cookie.split("; ").find((part) => part.startsWith("devflow_csrf="))?.split("=").slice(1).join("=") || "";
}

export async function api<T>(path: string, body?: unknown, method?: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api/v1${path}`, {
      method: method || (body === undefined ? "GET" : "POST"), credentials: "same-origin",
      signal: AbortSignal.timeout(path.endsWith("/probe") ? 65000 : 15000),
      headers: body === undefined ? {} : { "Content-Type": "application/json", "X-CSRF-Token": decodeURIComponent(csrfToken()) },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError("NETWORK_ERROR", "无法连接服务，请检查后端是否已启动", 0);
  }
  if (response.status === 204) return undefined as T;
  let payload;
  try { payload = await response.json(); }
  catch { throw new ApiError("INVALID_RESPONSE", "服务返回格式不正确，请检查后端连接", response.status); }
  if (!response.ok) throw new ApiError(payload.error?.code || "REQUEST_FAILED", payload.error?.message || "请求未完成", response.status);
  return payload.data as T;
}

export async function apiUpload<T>(path: string, file: File, fields?: Record<string, string>): Promise<T> {
  const form = new FormData();
  form.append("file", file);
  for (const [key, value] of Object.entries(fields || {})) form.append(key, value);
  let response: Response;
  try {
    response = await fetch(`/api/v1${path}`, {
      method: "POST", credentials: "same-origin", body: form,
      headers: { "X-CSRF-Token": decodeURIComponent(csrfToken()) },
      signal: AbortSignal.timeout(65000),
    });
  } catch {
    throw new ApiError("NETWORK_ERROR", "上传失败：无法连接服务", 0);
  }
  let payload;
  try { payload = await response.json(); }
  catch { throw new ApiError("INVALID_RESPONSE", "服务返回格式不正确", response.status); }
  if (!response.ok) throw new ApiError(payload.error?.code || "REQUEST_FAILED", payload.error?.message || "上传未完成", response.status);
  return payload.data as T;
}
