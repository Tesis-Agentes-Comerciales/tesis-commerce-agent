import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect, fn } from "storybook/test"

import { OPEN_GATES } from "./gates.ts"
import { SnapshotReadError } from "./snapshot-reader.ts"
import { SourceView } from "./SourceMonitor.tsx"
import {
  acceptLocalRead,
  beginLocalRead,
  offlineSource,
  rejectLocalRead,
  sourceDescription,
  type SourceState,
} from "./source-state.ts"
import { DEMO_SNAPSHOT, MONITOR_BUNDLE, STORY_READ_AT } from "@/storybook/governance"

// Cada estado sale de las mismas transiciones que usa `SourceMonitor` (source-state.ts).
const offline = offlineSource(MONITOR_BUNDLE)
const read = acceptLocalRead(DEMO_SNAPSHOT, STORY_READ_AT)

const meta = {
  title: "Governance/SourceView",
  component: SourceView,
  args: { state: offline, onOffline: fn(), onLocal: fn() },
  parameters: { layout: "fullscreen" },
} satisfies Meta<typeof SourceView>

export default meta
type Story = StoryObj<typeof meta>

type PlayContext = Parameters<NonNullable<Story["play"]>>[0]

/**
 * El chip de la fuente describe el estado; desconectado e inconsistente se anuncian como alerta y
 * el resto como estado. La evidencia solo aparece cuando hay un bundle completo.
 */
async function expectSource({ canvas, args }: PlayContext, state: SourceState) {
  const role = state.status === "inconsistent" || state.status === "disconnected" ? "alert" : "status"
  await expect(canvas.getByRole(role)).toHaveTextContent(sourceDescription(state))
  await expect(canvas.getByText(/Snapshot local sin autenticación real/)).toBeVisible()
  await expect(canvas.getAllByText(OPEN_GATES).length).toBeGreaterThan(0)
  const monitor = canvas.queryByRole("region", { name: "Monitor de gobernanza" })
  await expect(monitor !== null).toBe(state.bundle !== null)
  await expect(canvas.queryByText(/Hora de lectura del navegador/) !== null).toBe(state.readAt !== null)
  await expect(canvas.getByRole("button", { name: "Reproducción offline" })).toHaveAttribute(
    "aria-pressed",
    String(state.mode === "offline"),
  )
  await expect(args.onLocal).not.toHaveBeenCalled()
}

/** Modo inicial: reproducción offline explícita del bundle versionado, sin lectura HTTP. */
export const Offline: Story = {
  play: async (context) => {
    await expectSource(context, offline)
    await expect(context.canvas.queryByRole("button", { name: "Releer snapshot" })).toBeNull()
  },
}

/** Lectura en curso: no hay evidencia previa del snapshot que mostrar. */
export const Loading: Story = {
  args: { state: beginLocalRead(offline) },
  play: async (context) => expectSource(context, context.args.state),
}

/** Snapshot leído por HTTP local: fixture histórica, con la hora de lectura del navegador. */
export const Read: Story = {
  args: { state: read },
  play: async (context) => {
    await expectSource(context, read)
    await expect(context.canvas.getByText(new RegExp(STORY_READ_AT))).toBeVisible()
  },
}

/** Falló la relectura: se conserva la última lectura completa, marcada obsoleta. */
export const Stale: Story = {
  args: { state: rejectLocalRead(read, new SnapshotReadError("timeout")) },
  play: async (context) => {
    await expectSource(context, context.args.state)
    await expect(context.canvas.getByText(/Tiempo de lectura agotado/)).toBeVisible()
  },
}

/** Sin snapshot previo: no se cae a offline por su cuenta, hay que elegirlo. */
export const Disconnected: Story = {
  args: { state: rejectLocalRead(beginLocalRead(offline), new SnapshotReadError("disconnected")) },
  play: async (context) => {
    await expectSource(context, context.args.state)
    await expect(context.canvas.getByText(/No hay evidencia mostrable de esta lectura/)).toBeVisible()
  },
}

/** Snapshot que no cumple el hash o el contrato: se oculta toda la evidencia de esa fuente. */
export const Inconsistent: Story = {
  args: { state: rejectLocalRead(read, new SnapshotReadError("inconsistent")) },
  play: async (context) => expectSource(context, context.args.state),
}
