import { fileURLToPath } from "node:url"

import { storybookTest } from "@storybook/addon-vitest/vitest-plugin"
import { playwright } from "@vitest/browser-playwright"
import { defineConfig, mergeConfig } from "vitest/config"

import viteConfig from "./vite.config.ts"

// Cada historia es un test de Vitest en Chromium headless: render, `play` y a11y (addon-a11y).
// `npm run test` sigue siendo `node --test`; este archivo solo define el proyecto `storybook`.
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
      ],
    },
  }),
)
