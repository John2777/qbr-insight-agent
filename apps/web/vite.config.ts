import { defineConfig } from "vitest/config";
import { loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const proxyTarget = loadEnv(mode, ".", "").VITE_API_PROXY_TARGET ?? "http://localhost:8000";
  return {
    plugins: [react()],
    server: {
      port: 3000,
      proxy: {
        "/api": proxyTarget,
        "/health": proxyTarget
      }
    },
    test: {
      environment: "jsdom",
      setupFiles: "./src/test-setup.ts",
      include: ["src/**/*.test.ts", "src/**/*.test.tsx"]
    }
  };
});
