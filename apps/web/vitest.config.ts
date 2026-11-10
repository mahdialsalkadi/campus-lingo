import { fileURLToPath } from "node:url";

import { defineConfig } from "vitest/config";

/**
 * Vitest config for the frontend unit tests.
 *
 * `environment: "node"` keeps the suite dependency-free (no jsdom) — the
 * streaming helpers under test only need the global `fetch`/`Response`/
 * `ReadableStream` that Node 18+ provides. The `@` alias mirrors the tsconfig
 * `paths` entry so tests can import the same `@/lib/*` modules the app uses.
 */
export default defineConfig({
  resolve: {
    alias: {
      "@": fileURLToPath(new URL(".", import.meta.url)),
    },
  },
  test: {
    environment: "node",
    include: ["test/**/*.test.ts"],
  },
});
