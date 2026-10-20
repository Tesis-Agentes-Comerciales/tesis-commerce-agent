import { DEMO_SNAPSHOT_ID } from "./snapshot-contract.ts"

/** Local demo settings, defined only here; each default can be overridden by its environment variable.
 * The UI build sees only `VITE_` variables, so the snapshot port keeps that prefix for the server too. */
export const DEMO_DEFAULTS = {
  VITE_GOVERNANCE_SNAPSHOT_PORT: "5183",
  GOVERNANCE_SNAPSHOT_ORIGINS: "http://127.0.0.1:5182,http://127.0.0.1:5179",
  GOVERNANCE_DEMO_DIR: ".local/governance-demo",
} as const
// Loopback is part of the demo boundary, not a setting.
export const DEMO_HOST = "127.0.0.1"

export function demoConfig(env: Record<string, unknown> = {}) {
  const read = (key: keyof typeof DEMO_DEFAULTS) => {
    const value = env[key]
    return typeof value === "string" && value.trim() ? value.trim() : DEMO_DEFAULTS[key]
  }
  const port = Number(read("VITE_GOVERNANCE_SNAPSHOT_PORT"))
  if (!Number.isInteger(port) || port < 1 || port > 65_535) throw Error("Puerto de snapshot inválido")
  const origins = read("GOVERNANCE_SNAPSHOT_ORIGINS").split(",").map((origin) => origin.trim()).filter(Boolean)
  // CORS may only widen which loopback pages read the fixture, never admit a remote origin.
  if (origins.some((origin) => !URL.canParse(origin) || new URL(origin).origin !== origin || !["127.0.0.1", "localhost"].includes(new URL(origin).hostname))) throw Error("Origen de snapshot inválido")
  return { port, snapshotUrl: `http://${DEMO_HOST}:${port}/snapshots/${DEMO_SNAPSHOT_ID}`, origins, directory: read("GOVERNANCE_DEMO_DIR") }
}
