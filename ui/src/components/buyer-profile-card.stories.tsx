import type { Meta, StoryObj } from "@storybook/react-vite"
import { useState } from "react"
import { fn } from "storybook/test"

import { BuyerProfileCard } from "@/components/buyer-profile-card"
import { DEMO_PROFILE, type BuyerProfile } from "@/lib/commerce-types"

type ProfileStoryArgs = {
  profile: BuyerProfile
  onChange: (profile: BuyerProfile) => void
}

function StatefulProfileCard({ profile: initialProfile, onChange }: ProfileStoryArgs) {
  const [profile, setProfile] = useState(initialProfile)

  return (
    <div className="w-[min(28rem,calc(100vw-2rem))] overflow-hidden rounded-xl bg-white shadow-lg ring-1 ring-black/5">
      <BuyerProfileCard
        profile={profile}
        onChange={(next) => {
          onChange(next)
          setProfile(next)
        }}
      />
    </div>
  )
}

const meta = {
  title: "Purchase/BuyerProfileCard",
  component: BuyerProfileCard,
  tags: ["autodocs"],
  args: {
    profile: DEMO_PROFILE,
    onChange: fn(),
  },
  render: (args) => <StatefulProfileCard key={JSON.stringify(args.profile)} {...args} />,
} satisfies Meta<typeof BuyerProfileCard>

export default meta
type Story = StoryObj<typeof meta>

export const Default: Story = {}

export const Empty: Story = {
  args: {
    profile: Object.fromEntries(
      Object.keys(DEMO_PROFILE).map((field) => [field, ""]),
    ) as BuyerProfile,
  },
}
