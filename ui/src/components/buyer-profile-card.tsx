import { CheckCircle2Icon, MapPinIcon } from "lucide-react"

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import type { BuyerProfile } from "@/lib/commerce-types"

type ProfileField = keyof BuyerProfile

type BuyerProfileCardProps = {
  profile: BuyerProfile
  onChange: (profile: BuyerProfile) => void
}

export function BuyerProfileCard({ profile, onChange }: BuyerProfileCardProps) {
  const update = (field: ProfileField, value: string) => {
    onChange({ ...profile, [field]: value })
  }

  return (
    <Card className="rounded-xl border-0 bg-white shadow-none ring-0" data-testid="buyer-profile">
      <CardHeader className="border-b border-border/80">
        <div className="flex items-center justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2 text-base font-semibold">
              <span className="flex size-7 items-center justify-center rounded-full bg-muted">
                <MapPinIcon className="size-3.5 text-foreground" aria-hidden />
              </span>
              Datos de entrega
            </CardTitle>
            <CardDescription className="mt-1 leading-5">
              Perfil sintético para el checkout sandbox.
            </CardDescription>
          </div>
          <CheckCircle2Icon className="size-4.5 text-emerald-600" aria-label="Perfil completo" />
        </div>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 gap-3">
          <ProfileInput
            id="firstName"
            label="Nombre"
            value={profile.firstName}
            onChange={update}
          />
          <ProfileInput
            id="lastName"
            label="Apellido"
            value={profile.lastName}
            onChange={update}
          />
          <ProfileInput
            id="email"
            label="Email"
            type="email"
            value={profile.email}
            onChange={update}
            className="col-span-2"
          />
          <ProfileInput
            id="phoneNumber"
            label="Teléfono"
            type="tel"
            value={profile.phoneNumber}
            onChange={update}
            className="col-span-2"
          />
          <ProfileInput
            id="addressLine"
            label="Dirección"
            value={profile.addressLine}
            onChange={update}
            className="col-span-2"
          />
          <ProfileInput id="city" label="Ciudad" value={profile.city} onChange={update} />
          <ProfileInput id="state" label="Estado" value={profile.state} onChange={update} />
          <ProfileInput
            id="country"
            label="País"
            value={profile.country}
            onChange={update}
          />
          <ProfileInput
            id="postalCode"
            label="Código postal"
            value={profile.postalCode}
            onChange={update}
          />
        </div>
        <p className="mt-4 border-t border-border pt-3 text-[0.68rem] leading-4 text-muted-foreground">
          Los datos viajan al agente y al merchant. No se incluyen en el prompt del modelo.
        </p>
      </CardContent>
    </Card>
  )
}

type ProfileInputProps = {
  id: ProfileField
  label: string
  value: string
  onChange: (field: ProfileField, value: string) => void
  type?: string
  className?: string
}

function ProfileInput({
  id,
  label,
  value,
  onChange,
  type = "text",
  className,
}: ProfileInputProps) {
  return (
    <div className={`flex flex-col gap-1.5 ${className ?? ""}`}>
      <Label htmlFor={id} className="text-xs text-muted-foreground">
        {label}
      </Label>
      <Input
        id={id}
        type={type}
        required
        autoComplete="off"
        value={value}
        onChange={(event) => onChange(id, event.target.value)}
        className="h-10 rounded-xl border-[#e5e5e5] bg-white px-3 focus-visible:border-foreground/30 focus-visible:ring-foreground/10"
      />
    </div>
  )
}
