import { DEMO_PROFILE, type BuyerProfile } from "@/lib/commerce-types"

// Los datos de entrega sobreviven a una recarga en este navegador, por cuenta. Viajan al backend
// para el checkout y nunca al modelo. Sin almacenamiento (ventana privada, sitio bloqueado) o con un
// valor que no tiene la forma del perfil, se usa `DEMO_PROFILE`.

/** Lo único que se usa de `localStorage`; las pruebas pasan uno en memoria. */
export type ProfileStorage = Pick<Storage, "getItem" | "setItem">

const PROFILE_FIELDS = Object.keys(DEMO_PROFILE) as Array<keyof BuyerProfile>

export function profileStorageKey(userId: string) {
  return `comprador-acp:datos-de-entrega:${userId}`
}

function browserStorage(): ProfileStorage | null {
  try {
    return window.localStorage
  } catch {
    return null
  }
}

/** El perfil guardado si cada campo es texto; solo sus campos, cualquier otro se descarta. */
export function parseBuyerProfile(value: unknown): BuyerProfile | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null
  const stored = value as Record<string, unknown>
  const profile = { ...DEMO_PROFILE }
  for (const field of PROFILE_FIELDS) {
    const text = stored[field]
    if (typeof text !== "string") return null
    profile[field] = text
  }
  return profile
}

export function loadBuyerProfile(
  userId: string,
  storage: ProfileStorage | null = browserStorage(),
): BuyerProfile {
  try {
    const saved = storage?.getItem(profileStorageKey(userId))
    if (!saved) return DEMO_PROFILE
    return parseBuyerProfile(JSON.parse(saved)) ?? DEMO_PROFILE
  } catch {
    return DEMO_PROFILE
  }
}

export function saveBuyerProfile(
  userId: string,
  profile: BuyerProfile,
  storage: ProfileStorage | null = browserStorage(),
) {
  try {
    storage?.setItem(profileStorageKey(userId), JSON.stringify(profile))
  } catch {
    // Sin espacio o sin permiso: el perfil sigue en memoria hasta recargar.
  }
}
