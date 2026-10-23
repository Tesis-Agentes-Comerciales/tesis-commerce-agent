import { createContext } from "react"

import type { PurchaseMode } from "@/components/purchase-mode-switch"
import type { BuyerProfile } from "@/lib/commerce-types"

/**
 * Lo que la app le da a «Delegaciones» cuando la API las expone (`hnp_enabled` en `/health`). Con
 * HNP apagado no hay anfitrión y no se monta nada: la UI HP queda igual.
 */
export type DelegationsHost = {
  /** «Mis delegaciones»: el diálogo con la lista, el estado y la revocación. */
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Datos de entrega del perfil: el formulario los propone y el comprador los cambia. */
  profile: BuyerProfile
  /** Un escalamiento vuelve a «Comprar conmigo»: la compra sigue en el chat con el comprador. */
  onPurchaseModeChange: (mode: PurchaseMode) => void
}

export const DelegationsHostContext = createContext<DelegationsHost | null>(null)
