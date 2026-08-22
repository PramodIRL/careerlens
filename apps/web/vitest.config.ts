import { fileURLToPath } from "node:url";

import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: {
    // Mirrors tsconfig.json's "@/*" path mapping — Next.js resolves that
    // natively, but Vite (which Vitest uses) doesn't read tsconfig
    // paths on its own.
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  test: {
    // jsdom (not "node") since Prompt 1.2 adds React component tests
    // for the auth pages/protected navigation — the health-status tests
    // are pure functions and run fine under jsdom too, so one shared
    // environment is simplest.
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
});
