"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { ApiError } from "@/lib/api/client";
import { retryRead } from "@/lib/read-retry";

export function useBackgroundRefresh(read: (signal: AbortSignal) => Promise<unknown>) {
  const router = useRouter();
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const lifetime = useRef<AbortController | null>(null);
  const pending = useRef(false);
  const inFlight = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    inFlight.current = false;
    pending.current = false;
    return () => { controller.abort(); };
  }, [read]);

  const refreshNow = useCallback(async () => {
    const controller = lifetime.current;
    if (!controller || controller.signal.aborted) return;
    if (inFlight.current) { pending.current = true; return; }
    inFlight.current = true;
    setRefreshing(true);
    setError("");
    try {
      do {
        pending.current = false;
        await retryRead(() => read(controller.signal), {
          signal: controller.signal,
          shouldRetry: (err) => err instanceof ApiError && (err.status === 0 || err.status === 429 || err.status >= 500),
          onRetry: () => setRetrying(true),
        });
      } while (pending.current && !controller.signal.aborted);
    } catch (err) {
      if (!controller.signal.aborted) {
        if (err instanceof ApiError && err.status === 401) router.replace("/login");
        else setError(`更新失败：${err instanceof Error ? err.message : "暂时无法连接服务"}。请重试加载。`);
      }
    } finally {
      if (lifetime.current === controller && !controller.signal.aborted) {
        inFlight.current = false;
        setRefreshing(false);
        setRetrying(false);
      }
    }
  }, [read, router]);

  return { error, refreshing, retrying, refreshNow };
}
