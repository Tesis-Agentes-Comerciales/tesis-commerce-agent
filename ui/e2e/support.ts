import type { Page } from "@playwright/test"

import { demoConfig } from "../src/features/governance-monitor/demo-config.ts"
import { BUYER_SESSION, BUYER_TOKEN } from "../src/storybook/fixtures.ts"

// Puertos y carpeta propios: el E2E nunca reutiliza el servidor de desarrollo, la vista de auditoría
// ni el directorio de la demo de snapshots.
export const UI_ORIGIN = "http://127.0.0.1:5190"
export const AGENT_API_URL = "http://127.0.0.1:43120"
export const SNAPSHOT_ENV = {
  VITE_GOVERNANCE_SNAPSHOT_PORT: "5191",
  GOVERNANCE_SNAPSHOT_ORIGINS: UI_ORIGIN,
  GOVERNANCE_DEMO_DIR: ".local/governance-e2e",
}
export const SNAPSHOT_URL = demoConfig(SNAPSHOT_ENV).snapshotUrl

/**
 * Better Auth simulado en la red: el bundle usa su cliente real, pero `get-session` y `token`
 * responden con la sesión sintética de `src/storybook/fixtures.ts` y nada llega al servicio de acceso.
 */
export async function routeSession(page: Page, { signedIn = true } = {}) {
  await page.route("**/api/auth/**", (route) => {
    const { pathname } = new URL(route.request().url())
    if (pathname === "/api/auth/get-session") {
      return route.fulfill({ contentType: "application/json", body: JSON.stringify(signedIn ? BUYER_SESSION : null) })
    }
    if (pathname === "/api/auth/token" && signedIn) return route.fulfill({ json: { token: BUYER_TOKEN } })
    return route.fulfill({ status: 401, json: { code: "UNAUTHORIZED" } })
  })
}

/** URLs que la página pidió a la API del agente, para comprobar que una vista no la usa. */
export function agentRequests(page: Page) {
  const urls: string[] = []
  page.on("request", (request) => {
    if (request.url().startsWith(AGENT_API_URL)) urls.push(request.url())
  })
  return urls
}

/** Sin desbordamiento horizontal: en móvil nada obliga a desplazar la página de lado. */
export function fitsViewport(page: Page) {
  return page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
}
