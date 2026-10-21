import { fileURLToPath } from "node:url"

import { storybookTest } from "@storybook/addon-vitest/vitest-plugin"
import { playwright } from "@vitest/browser-playwright"
import { defineConfig, mergeConfig } from "vitest/config"

import viteConfig from "./vite.config.ts"

// Dos proyectos. `storybook`: cada historia es un test de Vitest en Chromium headless (render,
// `play` y a11y con addon-a11y). `unit`: la lógica sin interfaz (`src/**/*.test.ts`) en Node.
export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      projects: [
        {
          extends: true,
          plugins: [
            storybookTest({
              configDir: fileURLToPath(new URL("./.storybook", import.meta.url)),
              storybookScript: "npm run storybook -- --no-open",
              storybookUrl: "http://127.0.0.1:6006",
            }),
          ],
          test: {
            name: "storybook",
            browser: {
              enabled: true,
              headless: true,
              provider: playwright({}),
              instances: [{ browser: "chromium" }],
            },
          },
        },
        {
          extends: true,
          test: { name: "unit", environment: "node", include: ["src/**/*.test.ts"] },
        },
      ],
    },
  }),
)
