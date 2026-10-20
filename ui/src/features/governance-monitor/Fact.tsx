import { unavailable } from "./adapter.ts"

/** One recorded value; absent values read as unavailable in the source, never as zero or false. */
export function Fact({ label, value }: { label: string; value: string | null }) {
  return <div className="space-y-1"><dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{label}</dt><dd className="break-words text-sm [overflow-wrap:anywhere]">{unavailable(value)}</dd></div>
}
