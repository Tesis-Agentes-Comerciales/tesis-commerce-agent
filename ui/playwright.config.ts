import { defineConfig, devices } from "@playwright/test"

import { AGENT_API_URL, UI_ORIGIN } from "./e2e/support.ts"

export default defineConfig({
  testDir: "e2e",
  forbidOnly: !!process.env.CI,
  reporter: "list",
  use: { baseURL: UI_ORIGIN, trace: "retain-on-failure" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    // Build de producción en dist-e2e/ para no pisar dist/. Las rutas del agente y de Better Auth
    // las simula cada spec.
    command:
      "npx vite build --outDir dist-e2e --emptyOutDir" +
      ` && npx vite preview --outDir dist-e2e --host 127.0.0.1 --port ${new URL(UI_ORIGIN).port} --strictPort`,
    url: UI_ORIGIN,
    env: { VITE_AGENT_API_URL: AGENT_API_URL },
    timeout: 120_000,
  },
})
