import { cn } from "cn"
import {
  ArrowDownIcon,
  ArrowRightIcon,
  ArrowUpIcon,
  ListChecksIcon,
  LoaderCircleIcon,
  XIcon,
} from "lucide-react"
import { useEffect, useId, useRef, useState, type FormEvent } from "react"

import { formatMoney } from "@/components/agent/agent-format"
import { focusRing, pillButton } from "@/components/agent/agent-styles"
import { useCardFocus } from "@/components/agent/use-card-focus"
import { FORM_COPY, VALIDITY_OPTIONS } from "@/components/delegation/delegation-copy"
import { CARD_SURFACE, CardHeader, ErrorNotice } from "@/components/delegation/delegation-parts"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import type { ProductCard } from "@/lib/agent-contract"
import { MAX_PREFERENCE_CHARS, type DelegationDraft } from "@/lib/delegation-draft"

const FIELD =
  "h-10 rounded-xl border-[#e5e5e5] bg-white px-3 focus-visible:border-foreground/30 focus-visible:ring-foreground/10"

export type DelegationFormProps = {
  /** Lo que el comprador ya vio en el chat; sin opciones no hay qué delegar. */
  offers: ProductCard[]
  /** Valores iniciales: los datos de entrega del perfil o lo último que se envió. */
  draft: DelegationDraft
  /** Mientras `POST /delegations/proposals` responde, el botón conserva el foco. */
  submitting?: boolean
  /** Qué falta en el formulario o el mensaje saneado de la API. */
  error?: string | null
  onSubmit?: (draft: DelegationDraft) => void
  onCancel?: () => void
  /** Ocupa el lugar del botón que lo abrió: el foco pasa a su título. */
  autoFocus?: boolean
  className?: string
}

/** Las opciones van aparte: su orden es el de la lista «Orden de compra», no el del formulario. */
function draftOf(form: HTMLFormElement, variantIds: string[], ordered: boolean): DelegationDraft {
  const data = new FormData(form)
  const text = (name: string) => String(data.get(name) ?? "")
  return {
    variantIds,
    total: text("total"),
    validForSeconds: Number(text("valid_for")),
    addressLine: text("address_line"),
    city: text("city"),
    state: text("state"),
    preference: text("preference"),
    ordered,
  }
}

type Step = -1 | 1

/** `order` con la opción de `index` un lugar más arriba (−1) o más abajo (1). */
function moved(order: string[], index: number, step: Step) {
  const next = order.filter((_, item) => item !== index)
  next.splice(index + step, 0, order[index]!)
  return next
}

/**
 * El alcance que el comprador propone (§3.2, paso 1): las opciones vistas en el chat, el total
 * máximo con envío, la vigencia, la entrega y una indicación propia. Con «Comprar en orden», las
 * opciones marcadas se ordenan con «Subir» y «Bajar» (p0-conditional-intent.md §3.10). Lo valida
 * y lo devuelve; el servidor responde con la propuesta exacta, que la tarjeta de consentimiento
 * muestra.
 */
export function DelegationForm({
  offers,
  draft,
  submitting = false,
  error,
  onSubmit,
  onCancel,
  autoFocus = false,
  className,
}: DelegationFormProps) {
  const id = useId()
  const { cardRef, titleRef } = useCardFocus<HTMLFormElement>("form", false, autoFocus)
  const field = (name: string) => `${id}-${name}`
  // Sin opciones no hay qué proponer: «Preparar» queda como mientras responde la API.
  const blocked = submitting || offers.length === 0
  const titleOf = (variantId: string) => {
    const offer = offers.find((item) => item.variant_id === variantId)
    return offer?.variant_title || offer?.title || variantId
  }
  // Las marcadas, en el orden en que se marcaron: con «Comprar en orden», ese es el orden.
  const [chosen, setChosen] = useState(() =>
    draft.variantIds.filter((variantId) => offers.some((offer) => offer.variant_id === variantId)),
  )
  const [ordered, setOrdered] = useState(draft.ordered)
  const [announcement, setAnnouncement] = useState("")
  // El botón pulsado sigue a su opción: al moverla, React puede sacar el nodo del DOM y el foco se
  // perdería. Solo después de mover, no al marcar una opción.
  const moveButtons = useRef(new Map<string, HTMLButtonElement>())
  const focusAfterMove = useRef<string | null>(null)
  useEffect(() => {
    const key = focusAfterMove.current
    focusAfterMove.current = null
    if (key) moveButtons.current.get(key)?.focus()
  }, [chosen])

  const check = (variantId: string, checked: boolean) =>
    setChosen((current) => {
      const rest = current.filter((item) => item !== variantId)
      return checked ? [...rest, variantId] : rest
    })

  const move = (index: number, step: Step) => {
    const variantId = chosen[index]!
    const next = moved(chosen, index, step)
    focusAfterMove.current = `${variantId}:${step}`
    setChosen(next)
    setAnnouncement(FORM_COPY.position(titleOf(variantId), index + step + 1, next.length))
  }

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!blocked) onSubmit?.(draftOf(event.currentTarget, chosen, ordered))
  }

  return (
    <form
      ref={cardRef}
      aria-labelledby={field("title")}
      aria-busy={submitting || undefined}
      noValidate
      onSubmit={submit}
      data-testid="delegation-form"
      className={cn(CARD_SURFACE, className)}
    >
      <CardHeader
        Icon={ListChecksIcon}
        tone="ink"
        title={FORM_COPY.title}
        titleId={field("title")}
        titleRef={titleRef}
        subtitle={FORM_COPY.subtitle}
      />

      <div className="space-y-5 p-5">
        {/* `min-w-0`: un fieldset mide al menos su contenido, y el orden a 320 px lo desbordaba. */}
        <fieldset className="min-w-0 space-y-2">
          <legend className="text-sm font-medium">{FORM_COPY.offers}</legend>
          {offers.length === 0 ? (
            <p className="text-sm leading-5 text-[#6b6b6b]">{FORM_COPY.noOffers}</p>
          ) : (
            <ul className="space-y-1.5">
              {offers.map((offer) => (
                <li key={offer.variant_id}>
                  <label className="flex items-start gap-3 rounded-[16px] border border-[#ececec] px-3.5 py-3 text-sm leading-5 has-checked:border-[#0d0d0d]">
                    <input
                      type="checkbox"
                      name="variant"
                      value={offer.variant_id}
                      checked={chosen.includes(offer.variant_id)}
                      onChange={(event) => check(offer.variant_id, event.currentTarget.checked)}
                      className="mt-0.5 size-4 shrink-0 accent-[#0d0d0d]"
                    />
                    <span className="min-w-0">
                      <span className="block font-medium wrap-break-word">{offer.variant_title}</span>
                      <span className="block text-xs text-[#6b6b6b] tabular-nums">
                        {FORM_COPY.catalogPrice(
                          formatMoney(offer.observed_unit_amount_minor, offer.currency),
                        )}
                      </span>
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          )}
          {/*
            Un interruptor y no otra casilla: cambia el formulario al instante (muestra el orden) y no
            es una opción que se compre.
          */}
          {offers.length > 0 && (
            <div className="flex items-start justify-between gap-3 px-1 pt-1.5 text-sm leading-5">
              <div className="min-w-0">
                <label htmlFor={field("ordered")} className="font-medium">
                  {FORM_COPY.ordered}
                </label>
                <p id={field("ordered-hint")} className="text-xs text-[#6b6b6b]">
                  {FORM_COPY.orderedHint}
                </p>
              </div>
              <button
                id={field("ordered")}
                type="button"
                role="switch"
                aria-checked={ordered}
                aria-describedby={field("ordered-hint")}
                onClick={() => setOrdered((current) => !current)}
                className={cn(
                  "group mt-0.5 inline-flex h-5 w-9 shrink-0 items-center rounded-full border border-[#8a8a8a] bg-white p-0.5 transition-colors aria-checked:border-[#0d0d0d] aria-checked:bg-[#0d0d0d]",
                  focusRing,
                )}
              >
                <span
                  aria-hidden
                  className="size-3.5 rounded-full bg-[#6b6b6b] transition-transform group-aria-checked:translate-x-4 group-aria-checked:bg-white motion-reduce:transition-none"
                />
              </button>
            </div>
          )}
          {ordered && chosen.length > 0 && (
            <ol aria-label={FORM_COPY.order} className="space-y-1.5">
              {chosen.map((variantId, index) => {
                const title = titleOf(variantId)
                const button = (step: Step) => {
                  const edge = step === -1 ? index === 0 : index === chosen.length - 1
                  const Icon = step === -1 ? ArrowUpIcon : ArrowDownIcon
                  const label = step === -1 ? FORM_COPY.moveUp(title) : FORM_COPY.moveDown(title)
                  return (
                    <button
                      type="button"
                      ref={(node) => {
                        const key = `${variantId}:${step}`
                        if (node) moveButtons.current.set(key, node)
                        else moveButtons.current.delete(key)
                      }}
                      aria-label={label}
                      title={label}
                      aria-disabled={edge || undefined}
                      onClick={edge ? undefined : () => move(index, step)}
                      className={cn(pillButton({ tone: "ghost", size: "icon" }), "size-8")}
                    >
                      <Icon aria-hidden />
                    </button>
                  )
                }
                return (
                  <li
                    key={variantId}
                    className="flex items-center gap-2 rounded-[16px] bg-[#f7f7f7] py-1 pr-1 pl-3 text-sm leading-5"
                  >
                    <span
                      aria-hidden
                      className="flex size-5 shrink-0 items-center justify-center rounded-full bg-[#0d0d0d] text-[11px] font-semibold text-white tabular-nums"
                    >
                      {index + 1}
                    </span>
                    <span className="min-w-0 flex-1 font-medium wrap-break-word">{title}</span>
                    <span className="flex shrink-0">
                      {button(-1)}
                      {button(1)}
                    </span>
                  </li>
                )
              })}
            </ol>
          )}
          <p role="status" className="sr-only">
            {announcement}
          </p>
        </fieldset>

        <div className="grid gap-3 @xs:grid-cols-2">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={field("total")} className="text-xs text-[#6b6b6b]">
              {FORM_COPY.total}
            </Label>
            <Input
              id={field("total")}
              name="total"
              inputMode="numeric"
              autoComplete="off"
              placeholder="2.000.000"
              defaultValue={draft.total}
              className={FIELD}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={field("valid_for")} className="text-xs text-[#6b6b6b]">
              {FORM_COPY.validity}
            </Label>
            <select
              id={field("valid_for")}
              name="valid_for"
              defaultValue={draft.validForSeconds}
              className={cn("w-full min-w-0 border text-base outline-none md:text-sm", FIELD)}
            >
              {VALIDITY_OPTIONS.map(({ seconds, label }) => (
                <option key={seconds} value={seconds}>
                  {label}
                </option>
              ))}
            </select>
          </div>
        </div>

        <fieldset className="space-y-3">
          <legend className="text-sm font-medium">{FORM_COPY.destination}</legend>
          <p className="text-xs leading-5 text-[#6b6b6b]">{FORM_COPY.destinationHint}</p>
          {(
            [
              ["address_line", FORM_COPY.addressLine, draft.addressLine],
              ["city", FORM_COPY.city, draft.city],
              ["state", FORM_COPY.state, draft.state],
            ] as const
          ).map(([name, label, value]) => (
            <div key={name} className="flex flex-col gap-1.5">
              <Label htmlFor={field(name)} className="text-xs text-[#6b6b6b]">
                {label}
              </Label>
              <Input
                id={field(name)}
                name={name}
                autoComplete="off"
                defaultValue={value}
                className={FIELD}
              />
            </div>
          ))}
        </fieldset>

        <div className="flex flex-col gap-1.5">
          <Label htmlFor={field("preference")} className="text-xs text-[#6b6b6b]">
            {FORM_COPY.preference}
          </Label>
          <textarea
            id={field("preference")}
            name="preference"
            rows={2}
            maxLength={MAX_PREFERENCE_CHARS}
            defaultValue={draft.preference}
            className={cn("w-full min-w-0 resize-none border py-2 text-base outline-none md:text-sm", FIELD, "h-auto")}
          />
        </div>

        {error && <ErrorNotice>{error}</ErrorNotice>}

        {/*
          `aria-disabled` y no `disabled`: el botón pulsado conserva el foco. Lado a lado miden unos
          370 px (más con DejaVu Sans en Linux): van juntos solo desde 28rem de tarjeta. A 390 px de
          teléfono la tarjeta mide 324 y «Preparar la autorización» quedaba cortado (#134).
        */}
        <div className="grid grid-cols-1 gap-2 @md:grid-cols-[auto_1fr]">
          <button
            type="button"
            onClick={submitting ? undefined : onCancel}
            aria-disabled={submitting || undefined}
            className={pillButton({ tone: "soft", size: "lg" })}
          >
            <XIcon aria-hidden />
            {FORM_COPY.cancel}
          </button>
          <button
            type="submit"
            aria-disabled={blocked || undefined}
            className={cn(pillButton({ tone: "ink", size: "lg" }), "w-full")}
          >
            {submitting ? (
              <LoaderCircleIcon className="motion-safe:animate-spin" aria-hidden />
            ) : (
              <ArrowRightIcon aria-hidden />
            )}
            {FORM_COPY.submit}
          </button>
        </div>
      </div>
    </form>
  )
}
