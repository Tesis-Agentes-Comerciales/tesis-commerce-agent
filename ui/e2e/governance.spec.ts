import { readFileSync } from "node:fs"
import { expect, test, type Page } from "@playwright/test"

import { EXPERIMENT_NOTICE, parseEvidence, SIMULATED_TRANSPORT_NOTICE } from "../src/features/governance-monitor/adapter.ts"
import { MONITOR_TITLE } from "../src/features/governance-monitor/gates.ts"
import { agentRequests, routeSession, SNAPSHOT_URL } from "./support.ts"

// `/governance` del build con VITE_GOVERNANCE_MONITOR=true. «Snapshot backend local» lee el servidor
// real de scripts/governance_snapshot_server.mjs, que playwright.config.ts levanta con su fixture.

const BUNDLE = parseEvidence(
  JSON.parse(readFileSync(new URL("../src/features/governance-monitor/evidence.json", import.meta.url), "utf8")),
)

const monitor = (page: Page) => page.getByRole("region", { name: "Monitor de gobernanza" })
const source = (page: Page, name: string) => page.getByRole("button", { name, exact: true })

async function readSnapshot(page: Page) {
  await source(page, "Snapshot backend local").click()
  await expect(page.getByRole("status")).toHaveText(/^Snapshot leído por HTTP local/)
  await expect(monitor(page)).toBeVisible()
}

test("sin sesión muestra el acceso y ninguna evidencia", async ({ page }) => {
  await routeSession(page, { signedIn: false })
  await page.goto("/governance")
  await expect(page.getByRole("button", { name: "Entrar" })).toBeVisible()
  await expect(monitor(page)).toHaveCount(0)
  for (const scenario of BUNDLE.scenarios) await expect(page.getByText(scenario.label)).toHaveCount(0)
})

test("abre en reproducción offline y lee el snapshot del servidor local", async ({ page }) => {
  await routeSession(page)
  const requests = agentRequests(page)
  await page.goto("/governance")
  await expect(page).toHaveTitle(MONITOR_TITLE)
  await expect(page.getByRole("status")).toHaveText(/^Reproducción offline/)
  await expect(source(page, "Reproducción offline")).toHaveAttribute("aria-pressed", "true")
  await expect(monitor(page)).toBeVisible()

  await readSnapshot(page)
  await expect(source(page, "Snapshot backend local")).toHaveAttribute("aria-pressed", "true")
  await expect(page.getByText(/Hora de lectura del navegador/)).toBeVisible()

  // El experimento HNP-02 abre con sus dos avisos, también desde el snapshot.
  const [experiment] = BUNDLE.experiments
  await page.getByRole("button", { name: experiment.label }).click()
  const detail = page.getByRole("region", { name: "Detalle del experimento" })
  await expect(detail.getByText(EXPERIMENT_NOTICE)).toBeVisible()
  await expect(detail.getByText(SIMULATED_TRANSPORT_NOTICE)).toBeVisible()
  await expect(page.locator("body")).not.toContainText(/permitid/i)

  // El monitor no importa el runtime comercial: nunca llama a la API del agente.
  expect(requests).toEqual([])
})

test("si la relectura falla conserva la última lectura marcada obsoleta", async ({ page }) => {
  await routeSession(page)
  await page.goto("/governance")
  await readSnapshot(page)
  await page.route(SNAPSHOT_URL, (route) => route.abort("connectionrefused"))
  await source(page, "Releer snapshot").click()
  await expect(page.getByRole("status")).toHaveText(/última lectura obsoleta/)
  await expect(monitor(page)).toBeVisible()
})

test("sin servidor de snapshots no hay evidencia hasta volver a offline", async ({ page }) => {
  await routeSession(page)
  await page.route(SNAPSHOT_URL, (route) => route.abort("connectionrefused"))
  await page.goto("/governance")
  await source(page, "Snapshot backend local").click()
  await expect(page.getByRole("alert")).toHaveText(/no se obtuvo un snapshot/)
  await expect(monitor(page)).toHaveCount(0)
  await source(page, "Reproducción offline").click()
  await expect(page.getByRole("status")).toHaveText(/^Reproducción offline/)
  await expect(monitor(page)).toBeVisible()
})
