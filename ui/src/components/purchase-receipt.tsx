import {
  CheckIcon,
  ExternalLinkIcon,
  LoaderCircleIcon,
  RotateCcwIcon,
  XIcon,
} from "lucide-react"
import { useState } from "react"

import { Button } from "@/components/ui/button"
import { approvePurchase, cancelPurchase, completePurchase } from "@/lib/commerce-api"
import type { PurchaseFlow } from "@/lib/commerce-types"

type PurchaseReceiptProps = {
  flow: PurchaseFlow
  onChange: (flow: PurchaseFlow) => void
  onReset: () => void
}

function money(amountMinor: number, currency: string) {
  return new Intl.NumberFormat("es-CO", {
    style: "currency",
    currency: currency.toUpperCase(),
  }).format(amountMinor / 100)
}

function compactFingerprint(value: string) {
  return `${value.slice(0, 10)}…${value.slice(-10)}`
}

export function PurchaseReceipt({ flow, onChange, onReset }: PurchaseReceiptProps) {
  const [workingError, setWorkingError] = useState<string | null>(null)
  const busy = ["approving", "canceling", "completing"].includes(flow.status)
  const preparation = flow.preparation
  const terms = flow.terms

  const submit = async () => {
    if (!preparation || !terms || busy) return
    setWorkingError(null)
    let approvalRecorded = flow.approvalRecorded ?? false

    try {
      if (!approvalRecorded) {
        onChange({ ...flow, status: "approving", error: undefined })
        await approvePurchase(preparation.run_id, terms.fingerprint)
        approvalRecorded = true
      }

      onChange({ ...flow, status: "completing", approvalRecorded, error: undefined })
      const order = await completePurchase(preparation.run_id)
      onChange({ ...flow, status: "completed", approvalRecorded, order, error: undefined })
    } catch (error) {
      const message = error instanceof Error ? error.message : "No se pudo completar la compra."
      setWorkingError(message)
      onChange({ ...flow, status: "error", approvalRecorded, error: message })
    }
  }

  const cancel = async () => {
    if (!preparation || busy || flow.approvalRecorded) return
    setWorkingError(null)
    try {
      onChange({ ...flow, status: "canceling", error: undefined })
      await cancelPurchase(preparation.run_id)
      onReset()
    } catch (error) {
      const message = error instanceof Error ? error.message : "No se pudo cancelar la compra."
      setWorkingError(message)
      onChange({ ...flow, status: "error", error: message })
    }
  }

  if (flow.status === "idle") return null

  if (flow.status === "preparing") {
    return (
      <div className="flex items-center gap-3 p-6" data-testid="receipt-loading">
        <LoaderCircleIcon className="size-5 animate-spin" aria-hidden />
        <p className="text-sm">Preparando el checkout…</p>
      </div>
    )
  }

  if (!preparation || !terms) {
    return (
      <div className="p-6" data-testid="receipt-error">
        <h2 className="text-lg font-semibold">No se pudo preparar la compra</h2>
        <p className="mt-2 text-sm leading-6 text-[#6b6b6b]">
          {flow.error ?? "Intenta con una solicitud diferente."}
        </p>
      </div>
    )
  }

  if (flow.status === "completed" && flow.order) {
    return (
      <section className="p-6" data-testid="receipt-completed">
        <div className="flex size-10 items-center justify-center rounded-full bg-emerald-600 text-white">
          <CheckIcon className="size-5" aria-hidden />
        </div>
        <h2 className="mt-5 text-xl font-semibold tracking-[-0.02em]">Compra completada</h2>
        <p className="mt-1 text-sm text-[#6b6b6b]">
          Orden {flow.order.order.order_number ?? flow.order.order.id}
        </p>

        <div className="my-6 border-y border-[#ececec] py-4">
          <ReceiptRow label="Estado" value={flow.order.order.status ?? "confirmed"} />
          <ReceiptRow label="Total" value={money(terms.total_minor, terms.currency)} strong />
        </div>

        <div className="grid grid-cols-2 gap-2">
          <Button type="button" variant="secondary" onClick={onReset} className="rounded-full">
            <RotateCcwIcon data-icon="inline-start" aria-hidden />
            Nueva compra
          </Button>
          <Button asChild className="rounded-full bg-[#0d0d0d] text-white">
            <a href={flow.order.order.permalink_url} target="_blank" rel="noreferrer">
              Ver orden
              <ExternalLinkIcon data-icon="inline-end" aria-hidden />
            </a>
          </Button>
        </div>
      </section>
    )
  }

  const retryingCompletion = flow.approvalRecorded === true

  return (
    <section className="p-6" data-testid="receipt-pending">
      <p className="text-xs font-medium text-[#6b6b6b]">CONFIRMAR COMPRA</p>
      <h2 className="mt-3 pr-8 text-xl font-semibold leading-7 tracking-[-0.02em]">
        {preparation.selected_candidate.product_title}
      </h2>
      <p className="mt-1 text-sm text-[#6b6b6b]">
        {preparation.selected_candidate.variant_title}
      </p>
      <p className="mt-5 text-3xl font-semibold tracking-[-0.04em]">
        {money(terms.total_minor, terms.currency)}
      </p>

      <div className="my-6 space-y-3 border-y border-[#ececec] py-4 text-sm">
        {terms.lines.map((line) => (
          <ReceiptRow
            key={line.item_id}
            label={`${line.quantity} × ${line.name}`}
            value={money(line.unit_amount_minor, terms.currency)}
          />
        ))}
        <ReceiptRow
          label="Entrega"
          value={`${terms.fulfillment_details?.address?.city ?? "—"}, ${terms.fulfillment_details?.address?.country ?? "—"}`}
        />
        <ReceiptRow label="Pago" value="Sandbox" />
      </div>

      <details className="mb-5 text-xs text-[#6b6b6b]">
        <summary className="cursor-pointer select-none font-medium text-[#0d0d0d]">
          Detalles ACP
        </summary>
        <div className="mt-3 space-y-1 rounded-xl bg-[#f7f7f7] p-3 font-mono leading-5 break-all">
          <p>Checkout: {terms.checkout_id}</p>
          <p>Fingerprint: {compactFingerprint(terms.fingerprint)}</p>
          <p>Payment handler: {terms.payment_handler}</p>
          <p>Merchant: {terms.merchant_origin}</p>
        </div>
      </details>

      {(workingError ?? flow.error) && (
        <p role="alert" className="mb-4 text-sm leading-5 text-red-600">
          {workingError ?? flow.error}
        </p>
      )}

      <div className={retryingCompletion ? undefined : "grid grid-cols-[auto_1fr] gap-2"}>
        {!retryingCompletion && (
          <Button
            type="button"
            variant="secondary"
            onClick={cancel}
            disabled={busy}
            className="h-12 rounded-full"
          >
            {flow.status === "canceling" ? (
              <LoaderCircleIcon data-icon="inline-start" className="animate-spin" aria-hidden />
            ) : (
              <XIcon data-icon="inline-start" aria-hidden />
            )}
            Cancelar
          </Button>
        )}
        <Button
          type="button"
          onClick={submit}
          disabled={busy}
          className="h-12 w-full rounded-full bg-[#0d0d0d] text-white hover:bg-[#0d0d0d]/85"
          data-testid="approve-purchase"
        >
          {busy && flow.status !== "canceling" && (
            <LoaderCircleIcon data-icon="inline-start" className="animate-spin" aria-hidden />
          )}
          {flow.status === "approving"
            ? "Registrando aprobación…"
            : flow.status === "completing"
              ? "Completando compra…"
              : retryingCompletion
                ? "Reintentar compra"
                : `Aprobar y comprar · ${money(terms.total_minor, terms.currency)}`}
        </Button>
      </div>
      <p className="mt-3 text-center text-xs leading-5 text-[#6b6b6b]">
        Autoriza únicamente estos términos. No mueve dinero real.
      </p>
    </section>
  )
}

function ReceiptRow({
  label,
  value,
  strong = false,
}: {
  label: string
  value: string
  strong?: boolean
}) {
  return (
    <div className="flex items-baseline justify-between gap-4">
      <dt className="text-[#6b6b6b]">{label}</dt>
      <dd className={`text-right ${strong ? "font-semibold" : "font-medium"}`}>{value}</dd>
    </div>
  )
}
