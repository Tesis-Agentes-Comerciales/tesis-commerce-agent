import { cn } from "cn"
import { ArrowRightIcon, ListChecksIcon, LoaderCircleIcon, XIcon } from "lucide-react"
import { useId, type FormEvent } from "react"

import { formatMoney } from "@/components/agent/agent-format"
import { pillButton } from "@/components/agent/agent-styles"
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

function draftOf(form: HTMLFormElement): DelegationDraft {
  const data = new FormData(form)
  const text = (name: string) => String(data.get(name) ?? "")
  return {
    variantIds: data.getAll("variant").map(String),
    total: text("total"),
    validForSeconds: Number(text("valid_for")),
    addressLine: text("address_line"),
    city: text("city"),
    state: text("state"),
    preference: text("preference"),
  }
}

/**
 * El alcance que el comprador propone (§3.2, paso 1): las opciones vistas en el chat, el total
 * máximo con envío, la vigencia, la entrega y una indicación propia. Lo valida y lo devuelve; el
 * servidor responde con la propuesta exacta, que la tarjeta de consentimiento muestra.
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

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!blocked) onSubmit?.(draftOf(event.currentTarget))
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
        <fieldset className="space-y-2">
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
                      defaultChecked={draft.variantIds.includes(offer.variant_id)}
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

        {/* `aria-disabled` y no `disabled`: el botón pulsado conserva el foco. */}
        <div className="grid grid-cols-1 gap-2 @xs:grid-cols-[auto_1fr]">
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
