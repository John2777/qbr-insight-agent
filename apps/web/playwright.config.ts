import { defineConfig } from "@playwright/test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { resolvePythonExecutable } from "./e2e/python-executable";

const webRoot = dirname(fileURLToPath(import.meta.url));
const projectRoot = resolve(webRoot, "../..");
const pythonExecutable = resolvePythonExecutable(projectRoot);
const e2eDataDir = mkdtempSync(join(tmpdir(), "qbr-playwright-"));
const inheritedEnv = Object.fromEntries(
  Object.entries(process.env).filter((entry): entry is [string, string] => entry[1] !== undefined)
);
process.env.QBR_E2E_DATA_DIR = e2eDataDir;

export default defineConfig({
  testDir: "./e2e",
  globalTeardown: "./e2e/global-teardown.ts",
  use: {
    baseURL: "http://127.0.0.1:3010",
    channel: process.env.CI ? undefined : "chrome",
    trace: "retain-on-failure"
  },
  webServer: [
    {
      name: "api",
      command: `${JSON.stringify(pythonExecutable)} -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8010`,
      cwd: projectRoot,
      env: {
        ...inheritedEnv,
        QBR_DATA_DIR: e2eDataDir,
        DATABASE_PATH: join(e2eDataDir, "app.sqlite3"),
        OBJECT_STORE_PATH: join(e2eDataDir, "objects"),
        VECTOR_INDEX_DIR: join(e2eDataDir, "vector-indexes"),
        RUN_INLINE_WORKER: "true",
        WORKER_POLL_SECONDS: "0.05",
        RETRIEVAL_STRATEGY: "hybrid",
        EMBEDDING_PROVIDER: "hashing",
        AUTH_MODE: "demo",
        LOG_LEVEL: "WARNING"
      },
      port: 8010,
      reuseExistingServer: false,
      timeout: 120_000,
      gracefulShutdown: { signal: "SIGTERM", timeout: 5_000 }
    },
    {
      name: "web",
      command: "npm run dev -- --host 127.0.0.1 --port 3010",
      cwd: webRoot,
      env: { ...inheritedEnv, VITE_API_PROXY_TARGET: "http://127.0.0.1:8010" },
      port: 3010,
      reuseExistingServer: false,
      timeout: 120_000,
      gracefulShutdown: { signal: "SIGTERM", timeout: 5_000 }
    }
  ]
});
