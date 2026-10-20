import { useState } from "react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { OPEN_GATES } from "./gates.ts"
import { unavailable, scenarioIssue, scenarioBadge, executionText, creationText, recoveryText, sourceText, validatorText, type EvidenceBundle, type PhaseEvidence, type Scenario } from "./adapter.ts"
import { ExperimentDetails } from "./ExperimentDetails.tsx"
import { Fact } from "./Fact.tsx"

function Decision({ title, value, explanation }: { title: string; value: PhaseEvidence | null; explanation: string }) {
  return <Card><CardHeader><CardTitle>{title}</CardTitle><p className="text-sm text-muted-foreground">{explanation}</p></CardHeader><CardContent className="space-y-4">
    <dl className="space-y-4">
      <Fact label="Decisión registrada" value={value?.outcome ?? null} />
      <Fact label="Motivo registrado" value={value?.reason ?? null} />
      <Fact label="Decision ID" value={value?.decisionId ?? null} />
      <Fact label="Evidence ID" value={value?.evidenceId ?? null} />
    </dl>
    <details><summary className="cursor-pointer text-sm underline">Digest de evidencia</summary><p className="mt-2 break-all font-mono text-xs">{unavailable(value?.digest ?? null)}</p></details>
  </CardContent></Card>
}

export function ScenarioDetails({ scenario: s }: { scenario: Scenario }) {
  const issue = scenarioIssue(s)
  if (issue) return <section aria-label="Detalle del escenario"><h2>{s.label}</h2><p role="alert">Evidencia inconsistente: {issue}. No se presenta una creación confirmada.</p><p>{OPEN_GATES}</p></section>
  const confirmed = s.recovery === "confirmed" || (s.creation === "confirmed" && s.recovery === "not_applicable")
  const disagreement = s.validator?.checks.filter((c) => c.result === "pass" && s.backend.failures.includes(c.id)) ?? []
  const validatorCopy = validatorText(s)
  return <section aria-label="Detalle del escenario" className="space-y-5">
    <div className="flex flex-wrap items-center justify-between gap-3"><div><h2 className="text-2xl font-semibold">{s.label}</h2><p className="mt-1 text-sm text-muted-foreground">{s.scope}</p></div><span className={`rounded-full px-4 py-2 text-sm font-semibold ${confirmed ? "bg-emerald-100 text-emerald-950" : "bg-amber-100 text-amber-950"}`}>{scenarioBadge(s)}</span></div>
    <div className="grid gap-4 md:grid-cols-3">
      <Card><CardHeader><CardTitle>Contexto y propuesta</CardTitle></CardHeader><CardContent className="space-y-4">
        <dl className="space-y-4">
          <Fact label="Modo · ejecución" value={`${s.mode} · ${s.executionKind}`} />
          <Fact label="Tipo de fuente" value={sourceText(s)} />
          <Fact label="Intención original" value={s.intent} />
          <Fact label="Propuesta / variante" value={s.proposal} />
          <Fact label="Operación propuesta" value={s.proposedAction} />
          <Fact label="Total de la fuente" value={s.totalMinor === null ? null : `${s.totalMinor.toLocaleString("es-CO")} unidades menores · ${s.currency}`} />
        </dl>
        <details><summary className="cursor-pointer text-sm underline">Restricciones conservadas</summary><p className="mt-2 break-words font-mono text-xs">{unavailable(s.constraints)}</p></details>
      </CardContent></Card>
      <Decision title="Q · shadow" value={s.shadow} explanation="Observa. Un allow shadow no autoriza despacho." />
      <Decision title="D1 · enforce" value={s.enforce} explanation={s.mode === "HNP" ? "No ejecutado por este harness HNP; controles experimentales debajo." : "Separada de Q. Una referencia no sustituye un veredicto ausente."} />
    </div>
    {s.validator && <Card><CardHeader><CardTitle>{validatorCopy.title}</CardTitle><p className="text-sm text-muted-foreground">{validatorCopy.note}</p></CardHeader><CardContent>
      <dl className="flex flex-wrap gap-6"><Fact label="Veredicto del validador" value={s.validator.verdict} /><Fact label="Adaptador" value={s.validator.adapterId} /><Fact label="Decisión backend independiente" value={s.backend.code} /></dl>
      <p className="mt-4 text-sm">{validatorCopy.checks}</p>
      <div className="mt-2 flex flex-wrap gap-2" aria-label="Declaraciones del validador">{s.validator.checks.map((c) => <span key={c.id} className={`rounded-md px-2 py-1 font-mono text-xs ${disagreement.some((x) => x.id === c.id) ? "bg-amber-100 text-amber-950" : "bg-muted"}`}>{c.id}: {c.result} (validador)</span>)}</div>
      <p className="mt-4 font-medium text-amber-900">{s.backend.failures.length ? `Control backend: ${s.backend.failures.join(", ")}. La evaluación del validador no superó el backend.` : "Sin fallos backend registrados."}</p>
      {disagreement.length > 0 && <div role="note" className="mt-3 rounded-lg border border-amber-400 bg-amber-50 p-3 text-sm text-amber-950"><strong>Discrepancia documentada entre validador y backend</strong><p>{disagreement.map((c) => `${c.id}: pass declarado por el validador; denegado por el backend`).join(". ")}. Se conservan ambas observaciones originales.</p>{s.totalMinor !== null && s.budgetLimitMinor !== null && <p>Total de la fuente: {s.totalMinor.toLocaleString("es-CO")}; límite de la fuente: {s.budgetLimitMinor.toLocaleString("es-CO")} unidades menores. {s.totalMinor > s.budgetLimitMinor ? "El total excede el límite." : "El total no excede el límite."}</p>}<p>{validatorCopy.falsePass}</p></div>}
      <p className="mt-2 text-sm text-muted-foreground">PDP ejecutado: {s.backend.executedPolicyIds?.length === 0 ? "ninguno; executed_policy_ids=[]" : "no disponible"}. No equivale a POL-03–10 ni D2/D3 operativo.</p>
    </CardContent></Card>}
    <Card><CardHeader><CardTitle>Operación y resultado observado</CardTitle></CardHeader><CardContent><dl className="grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
      <Fact label="Estado de ejecución" value={executionText[s.execution]} />
      <Fact label="Creación · observación inicial" value={creationText[s.creation]} />
      <Fact label="Estado de recuperación" value={recoveryText[s.recovery]} />
      <Fact label="POST comerciales históricos" value={s.commercialPosts === null ? null : String(s.commercialPosts)} />
      <Fact label={s.mode === "HNP" ? "Efecto en sink ficticio" : "Efecto checkout histórico"} value={s.effectOccurred === null ? null : s.effectOccurred ? "Observado" : "Ausencia de efectos observada en la fuente"} />
      <Fact label="Run / episodio" value={s.runId} /><Fact label="Operation ID" value={s.operationId} /><Fact label="Request ID" value={s.requestId} /><Fact label="Código backend registrado" value={s.backend.code} />
    </dl></CardContent></Card>
    <Card><CardHeader><CardTitle>Fuente y límites</CardTitle></CardHeader><CardContent className="space-y-4">
      <ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">{s.notes.map((note) => <li key={note}>{note}</li>)}</ul>
      {s.evidence.map((e) => <details key={e.artifact} className="rounded-lg border p-3"><summary className="cursor-pointer text-sm font-medium">{e.artifact} · ver procedencia</summary><dl className="mt-3 grid gap-3 sm:grid-cols-2"><Fact label="Puntero en artefacto" value={e.pointer} /><Fact label="SHA-256 del archivo original" value={e.sha256} /><Fact label="Commit de código de la ejecución" value={e.codeCommit} /><Fact label="Digest registrado" value={e.recordDigest} /></dl></details>)}
    </CardContent></Card>
  </section>
}

export function GovernanceMonitor({ bundle, embedded = false }: { bundle: EvidenceBundle; embedded?: boolean }) {
  const [selected, setSelected] = useState(bundle.scenarios[0].id)
  const experiment = bundle.experiments.find((e) => e.id === selected) ?? null
  const scenario = bundle.scenarios.find((s) => s.id === selected) ?? bundle.scenarios[0]
  return <section aria-label="Monitor de gobernanza" className={`${embedded ? "" : "h-svh overflow-y-auto"} bg-muted/30 px-4 py-8 text-foreground sm:px-8`}><div className="mx-auto max-w-6xl space-y-7">
    <header className="space-y-3"><div className="flex flex-wrap items-center gap-2 text-xs font-semibold uppercase tracking-widest"><span className="rounded-full bg-primary/10 px-3 py-1 text-primary">Solo lectura</span><span className="rounded-full bg-amber-100 px-3 py-1 text-amber-950">{bundle.status}</span><span>Sin LLM en vivo · sin ejecución comercial</span></div><h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">Monitor de gobernanza</h1><p className="max-w-3xl text-sm leading-relaxed text-muted-foreground">Reproducción de evidencia HP y HNP existente. Esta pantalla no autoriza, despacha ni completa compras. Los datos ausentes se muestran como no disponibles.</p></header>
    <nav aria-label="Escenarios de evidencia" className="flex flex-wrap gap-2">{bundle.scenarios.map((s) => <Button key={s.id} type="button" variant={!experiment && scenario.id === s.id ? "default" : "outline"} aria-pressed={!experiment && scenario.id === s.id} onClick={() => setSelected(s.id)}>{s.label}</Button>)}</nav>
    {bundle.experiments.length > 0 && <nav aria-label="Experimentos HNP-02" className="flex flex-wrap items-center gap-2"><span className="text-xs font-semibold uppercase tracking-widest text-muted-foreground">Experimentos · sink ficticio</span>{bundle.experiments.map((e) => <Button key={e.id} type="button" variant={experiment?.id === e.id ? "default" : "outline"} aria-pressed={experiment?.id === e.id} onClick={() => setSelected(e.id)}>{e.label}</Button>)}</nav>}
    {experiment ? <ExperimentDetails experiment={experiment} /> : <ScenarioDetails scenario={scenario} />}
    <footer className="border-t pt-4 text-xs leading-relaxed text-muted-foreground">HNP: {bundle.hnpLedgerEntriesVerified} entradas de ledger verificadas offline por el exportador; digests no equivalen a firmas. {OPEN_GATES} No es compra gobernada completa, UI humana de autorización ni validación de dos LLM.</footer>
  </div></section>
}
