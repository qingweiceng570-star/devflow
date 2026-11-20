import assert from "node:assert/strict";
import test from "node:test";
import { retryRead } from "../../src/lib/read-retry.ts";

test("reads recover from temporary failures and stop after three attempts", async () => {
  let calls = 0;
  const options = { signal: new AbortController().signal, shouldRetry: () => true, delays: [0, 0] };
  assert.equal(await retryRead(async () => { if (++calls < 3) throw new Error("offline"); return "ready"; }, options), "ready");
  assert.equal(calls, 3);
  calls = 0;
  await assert.rejects(retryRead(async () => { calls++; throw new Error("offline"); }, options));
  assert.equal(calls, 3);
});

test("authorization failures and abandoned views do not retry", async () => {
  let calls = 0;
  const controller = new AbortController();
  await assert.rejects(retryRead(async () => { calls++; throw new Error("forbidden"); }, {
    signal: controller.signal, shouldRetry: () => false,
  }));
  assert.equal(calls, 1);
  calls = 0;
  const pending = retryRead(async () => { calls++; throw new Error("offline"); }, {
    signal: controller.signal, shouldRetry: () => true, onRetry: () => queueMicrotask(() => controller.abort()),
  });
  await assert.rejects(pending);
  assert.equal(calls, 1);
});
