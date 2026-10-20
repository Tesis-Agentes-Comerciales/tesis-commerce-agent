import type { Meta, StoryObj } from "@storybook/react-vite"
import { expect } from "storybook/test"

import { SourceMonitor } from "./SourceMonitor.tsx"
import { MONITOR_BUNDLE, type SnapshotReply } from "@/storybook/governance"

// `governanceSnapshot` simula el servidor local de snapshots (src/storybook/governance.ts): cada
// lectura toma la siguiente respuesta. El lector real valida hash, tamaño y contrato.

const meta = {
  title: "Governance/SourceMonitor",
  component: SourceMonitor,
  args: { offlineBundle: MONITOR_BUNDLE },
  parameters: { layout: "fullscreen" },
} satisfies Meta<typeof SourceMonitor>

export default meta
type Story = StoryObj<typeof meta>

type PlayContext = Parameters<NonNullable<Story["play"]>>[0]

const snapshot = (...replies: SnapshotReply[]) => ({ governanceSnapshot: replies })

const LOCAL = { name: "Snapshot backend local" } as const
const REREAD = { name: "Releer snapshot" } as const
const OFFLINE = { name: "Reproducción offline" } as const

function monitor({ canvas }: PlayContext) {
  return canvas.queryByRole("region", { name: "Monitor de gobernanza" })
}

/** Lee el snapshot y espera a que el chip de la fuente lo confirme. */
async function readSnapshot(context: PlayContext) {
  await context.userEvent.click(context.canvas.getByRole("button", LOCAL))
  await expect(await context.canvas.findByText(/^Snapshot leído por HTTP local/)).toBeVisible()
}

/** Sin interacción la fuente es la reproducción offline; nunca lee el snapshot sola. */
export const OfflineByDefault: Story = {
  play: async (context) => {
    await expect(context.canvas.getByRole("status")).toHaveTextContent(/^Reproducción offline/)
    await expect(context.canvas.getByRole("button", OFFLINE)).toHaveAttribute("aria-pressed", "true")
    await expect(monitor(context)).toBeVisible()
  },
}

/** Lectura correcta: los bytes coinciden con el pin y el chip lo declara histórico, no en vivo. */
export const LocalRead: Story = {
  parameters: snapshot("snapshot"),
  play: async (context) => {
    await readSnapshot(context)
    await expect(context.canvas.getByText(/no es un evento en vivo/)).toBeVisible()
    await expect(context.canvas.getByText(/Hora de lectura del navegador/)).toBeVisible()
    await expect(context.canvas.getByRole("button", LOCAL)).toHaveAttribute("aria-pressed", "true")
    await expect(monitor(context)).toBeVisible()
  },
}

/** La relectura falla: queda la última lectura completa marcada obsoleta, sin mezclar datos. */
export const StaleAfterFailedReread: Story = {
  parameters: snapshot("snapshot", "unreachable"),
  play: async (context) => {
    await readSnapshot(context)
    await context.userEvent.click(context.canvas.getByRole("button", REREAD))
    await expect(await context.canvas.findByText(/última lectura obsoleta/)).toBeVisible()
    await expect(context.canvas.getByText(/Fuente desconectada\. No se mezclan datos/)).toBeVisible()
    await expect(monitor(context)).toBeVisible()
  },
}

/** Sin servidor no hay evidencia; volver a offline exige elegirlo. */
export const Disconnected: Story = {
  parameters: snapshot("unreachable"),
  play: async (context) => {
    const { canvas, userEvent } = context
    await userEvent.click(canvas.getByRole("button", LOCAL))
    await expect(await canvas.findByRole("alert")).toHaveTextContent(/no se obtuvo un snapshot/)
    await expect(monitor(context)).toBeNull()
    await userEvent.click(canvas.getByRole("button", OFFLINE))
    await expect(canvas.getByRole("status")).toHaveTextContent(/^Reproducción offline/)
    await expect(monitor(context)).toBeVisible()
  },
}

/** Un byte de más rompe el hash: el snapshot se rechaza entero y oculta la lectura anterior. */
export const Inconsistent: Story = {
  parameters: snapshot("snapshot", "altered"),
  play: async (context) => {
    await readSnapshot(context)
    await context.userEvent.click(context.canvas.getByRole("button", REREAD))
    await expect(await context.canvas.findByRole("alert")).toHaveTextContent(/snapshot rechazado íntegramente/)
    await expect(monitor(context)).toBeNull()
  },
}

/** El servidor rechaza su propia fixture (422): también es inconsistente, no desconectado. */
export const RejectedByServer: Story = {
  parameters: snapshot("rejected"),
  play: async (context) => {
    await context.userEvent.click(context.canvas.getByRole("button", LOCAL))
    await expect(await context.canvas.findByRole("alert")).toHaveTextContent(/Evidencia inconsistente/)
    await expect(monitor(context)).toBeNull()
  },
}
