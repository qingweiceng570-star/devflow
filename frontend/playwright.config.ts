import { defineConfig } from "@playwright/test";
import { existsSync } from "node:fs";

const candidates = [process.env.PLAYWRIGHT_EXECUTABLE_PATH, "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"].filter((path): path is string => !!path);
export default defineConfig({
  testDir: "./tests/e2e", workers: 1, fullyParallel: false,
  reporter: "list", timeout: 30000,
  use: {
    baseURL: process.env.DEVFLOW_WEB_URL || "http://localhost:3100", headless: true,
    launchOptions: { executablePath: candidates.find(existsSync) },
    screenshot: "only-on-failure", trace: "retain-on-failure",
  },
});
