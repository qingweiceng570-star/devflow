type RetryOptions = {
  signal: AbortSignal;
  shouldRetry: (error: unknown) => boolean;
  onRetry?: (attempt: number) => void;
  delays?: number[];
};

function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    signal.throwIfAborted();
    const aborted = () => { clearTimeout(timer); reject(signal.reason); };
    const timer = setTimeout(() => { signal.removeEventListener("abort", aborted); resolve(); }, ms);
    signal.addEventListener("abort", aborted, { once: true });
  });
}

// Only callers performing reads should use retries; mutations are never replayed.
export async function retryRead<T>(read: () => Promise<T>, options: RetryOptions): Promise<T> {
  const delays = options.delays ?? [500, 1500];
  for (let attempt = 0; ; attempt++) {
    options.signal.throwIfAborted();
    try { return await read(); }
    catch (error) {
      if (options.signal.aborted || attempt >= delays.length || !options.shouldRetry(error)) throw error;
      options.onRetry?.(attempt + 1);
      await pause(delays[attempt], options.signal);
    }
  }
}
