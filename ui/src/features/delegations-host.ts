import { createContext } from "react"

import type { BuyerProfile } from "@/lib/commerce-types"

/**
 * Lo que la app le da a «Delegaciones» cuando la API las expone (`hnp_enabled` en `/health`). Con
 * HNP apagado no hay anfitrión y no se monta nada: la UI HP queda igual.
 */
export type DelegationsHost = {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Datos de entrega del perfil: el formulario los propone y el comprador los cambia. */
  profile: BuyerProfile
}

export const DelegationsHostContext = createContext<DelegationsHost | null>(null)
