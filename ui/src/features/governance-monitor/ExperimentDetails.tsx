import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { OPEN_GATES } from "./gates.ts"
import { BUSINESS_CHECKS, unavailable, experimentIssue, experimentNotices, experimentKindText, validatorText, type Experiment, type ExperimentActor, type ExperimentCase, type ExperimentEpisode, type Latency, type PairTable, type Wilson } from "./adapter.ts"
import { Fact } from "./Fact.tsx"

const caseKindText = { admissible: "Admisible", changed_after_review: "Cambia tras la revisión", invalid: "Inválido", not_reviewable: "No revisable" } as const
const outcomeClassText = { experimental: "Experimental", infrastructure: "Fallo de infraestructura (fuera de denominadores)" } as const
const costBasisText = { fake_transport_estimate: "Estimación del transporte simulado; no es gasto real", provider_reported_usage: "Usage reportado por el proveedor" } as const
const armText = { A: "A · payload guionado + backend", B: "B · A + validador LLM" } as const

const number = (value: number, digits = 2) => value.toLocaleString("es-CO", { maximumFractionDigits: digits })
const percent = (value: number | null) => value === null ? "—" : `${number(value * 100, 1)} %`
const rate = (w: Wilson) => w.n === 0 ? "0/0 · sin denominador" : `${w.k}/${w.n} · ${percent(w.rate)} (IC 95 % ${percent(w.low)}–${percent(w.high)})`
const ms = (value: number) => `${number(value, 3)} ms`
const latency = (l: Latency) => l === null ? null : `mediana ${ms(l.medianMs)} · p95 ${ms(l.p95Ms)} · máx ${ms(l.maxMs)} (n=${l.n})`
// A scripted actor has no model settings; an LLM actor may leave one to the transport default.
const unset = (a: ExperimentActor) => a.kind === "scripted" ? "no aplica" : "no declarado"
const tally = (record: Partial<Record<string, number>>) => Object.entries(record).map(([key, n]) => `${key}: ${n}`).join(" · ") || "ninguno"

/** Rows are recorded aggregates; a missing value reads as unavailable, never as zero. */
function Table({ caption, head, rows }: { caption: string; head: string[]; rows: [string, ...(string | null)[]][] }) {
  return <div className="overflow-x-auto"><table className="w-full min-w-max text-left text-sm"><caption className="sr-only">{caption}</caption>
    <thead><tr>{head.map((h) => <th key={h} scope="col" className="border-b px-2 py-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">{h}</th>)}</tr></thead>
    <tbody>{rows.map(([label, ...cells]) => <tr key={label} className="border-b last:border-0"><th scope="row" className="px-2 py-2 font-medium">{label}</th>{cells.map((cell, i) => <td key={head[i + 1]} className="px-2 py-2 font-mono text-xs">{unavailable(cell)}</td>)}</tr>)}</tbody>
  </table></div>
}

function Episode({ arm, episode: x }: { arm: keyof typeof armText; episode: ExperimentEpisode }) {
  return <div className="space-y-3 rounded-lg border p-3"><h4 className="text-sm font-semibold">{armText[arm]}</h4><dl className="grid gap-3 sm:grid-cols-2">
    <Fact label="Episodio" value={x.episodeId} />
    <Fact label="Código de admisión" value={x.admissionCode} />
    <Fact label="Efecto en sink ficticio" value={x.effect ? "Simulado; sin operación real" : "Sin efecto"} />
    <Fact label="Clase de resultado" value={outcomeClassText[x.outcomeClass]} />
    <Fact label="Controles backend denegados" value={x.backendFailures.length ? x.backendFailures.join(", ") : "ninguno"} />
    <Fact label="Latencia del episodio" value={ms(x.latencyMs)} />
    {x.validator && <>
      <Fact label="Resultado del validador" value={x.validator.outcome} />
      <Fact label="Estado de la llamada" value={x.validator.callStatus ?? "sin llamada"} />
      <Fact label="Decisión declarada" value={x.validator.declaredDecision ?? "sin respuesta analizada"} />
      <Fact label="Tipo de error" value={x.validator.callStatus === null ? "sin llamada" : x.validator.errorKind ?? "ninguno"} />
      <Fact label="Razones declaradas (solo conteo)" value={x.validator.reasonCount === null ? "sin respuesta analizada" : String(x.validator.reasonCount)} />
      <Fact label="Latencia de la llamada" value={x.validator.latencyMs === null ? null : ms(x.validator.latencyMs)} />
      <Fact label="Tokens entrada · salida" value={x.validator.inputTokens === null || x.validator.outputTokens === null ? null : `${x.validator.inputTokens} · ${x.validator.outputTokens}`} />
      <Fact label="Costo de la llamada (USD)" value={x.validator.costUsd === null ? null : number(x.validator.costUsd, 7)} />
    </>}
  </dl></div>
}

function Declarations({ experiment: e, item: c }: { experiment: Experiment; item: ExperimentCase }) {
  const v = c.sample.B.validator
  if (!v) return null
  const copy = validatorText(e)
  if (v.nonPassChecks === null) return <p className="text-sm text-muted-foreground">Sin respuesta analizada del validador: no hay declaraciones por control.</p>
  const declared = v.nonPassChecks
  // Both arms of the pair ran the same backend checks; a denial there contradicts a declared pass.
  const denied = new Set([...c.sample.A.backendFailures, ...c.sample.B.backendFailures])
  const disagreement = BUSINESS_CHECKS.filter((id) => !declared[id] && denied.has(id))
  return <div className="space-y-2">
    <p className="text-sm">{copy.checks}</p>
    <div className="flex flex-wrap gap-2" aria-label="Declaraciones del validador">{BUSINESS_CHECKS.map((id) => <span key={id} className={`rounded-md px-2 py-1 font-mono text-xs ${disagreement.includes(id) ? "bg-amber-100 text-amber-950" : "bg-muted"}`}>{id}: {declared[id] ?? "pass"} (validador)</span>)}</div>
    {disagreement.length > 0 && <div role="note" className="rounded-lg border border-amber-400 bg-amber-50 p-3 text-sm text-amber-950"><strong>Discrepancia documentada entre validador y backend</strong><p>{disagreement.map((id) => `${id}: pass declarado por el validador; denegado por el backend del par`).join(". ")}. Se conservan ambas observaciones originales.</p><p>{copy.falsePass}</p></div>}
  </div>
}

const pairRow = (label: string, p: PairTable): [string, ...string[]] => [label, String(p.pairs), String(p.bothAdmitted), String(p.bothBlocked), String(p.onlyBBlocked), String(p.onlyABlocked), number(p.mcnemarExactP, 4)]

export function ExperimentDetails({ experiment: e }: { experiment: Experiment }) {
  const notices = <div role="note" className="space-y-1 rounded-lg border border-amber-400 bg-amber-50 p-3 text-sm font-medium text-amber-950">{experimentNotices(e).map((notice) => <p key={notice}>{notice}</p>)}</div>
  const issue = experimentIssue(e)
  if (issue) return <section aria-label="Detalle del experimento" className="space-y-4">{notices}<h2>{e.label}</h2><p role="alert">Evidencia inconsistente: {issue}. No se presentan cifras del experimento.</p><p>{OPEN_GATES}</p></section>
  const copy = validatorText(e), v = e.validator, cost = e.cost
  return <section aria-label="Detalle del experimento" className="space-y-5">
    {notices}
    <div className="flex flex-wrap items-center justify-between gap-3"><div><h2 className="text-2xl font-semibold">{e.label}</h2><p className="mt-1 text-sm text-muted-foreground">{e.scope}</p></div><span className="rounded-full bg-amber-100 px-4 py-2 text-sm font-semibold text-amber-950">Experimento · sin operación real</span></div>
    <Card><CardHeader><CardTitle>Corrida</CardTitle></CardHeader><CardContent><dl className="grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
      <Fact label="Modo · ejecución" value={`${e.mode} · ${e.executionKind}`} />
      <Fact label="Transporte" value={experimentKindText(e)} />
      <Fact label="Fase" value={e.phase} />
      <Fact label="Sink" value={`${e.sink} · sin efectos operativos`} />
      <Fact label="POST comerciales" value={String(e.commercialPosts)} />
      <Fact label="Creada (registro de la corrida)" value={e.createdAt} />
      <Fact label="Semilla · repeticiones" value={`${e.seed} · ${e.repetitions}`} />
      <Fact label="Casos excluidos de la matriz" value={e.excludedCases.length ? e.excludedCases.join(", ") : "ninguno"} />
    </dl></CardContent></Card>
    <Card><CardHeader><CardTitle>Actores</CardTitle><p className="text-sm text-muted-foreground">Configuración declarada por la corrida; sin prompts ni claves.</p></CardHeader><CardContent>
      <Table caption="Actores de la corrida" head={["Rol", "Tipo", "Adaptador", "Transporte", "Modelo", "Prompt", "Esfuerzo", "Temperatura", "Máx. tokens", "Timeout"]} rows={e.actors.map((a) => [a.role, a.kind, a.adapterId, a.transport, ...[a.model, a.promptVersion, a.reasoningEffort, a.temperature === null ? null : number(a.temperature), a.maxOutputTokens === null ? null : String(a.maxOutputTokens), a.timeoutSeconds === null ? null : `${number(a.timeoutSeconds)} s`].map((cell) => cell ?? unset(a))])} />
      {e.actors.filter((a) => a.promptSha256).map((a) => <details key={a.adapterId} className="mt-3"><summary className="cursor-pointer text-sm underline">SHA-256 del prompt · {a.adapterId}</summary><p className="mt-2 break-all font-mono text-xs">{a.promptSha256}</p></details>)}
    </CardContent></Card>
    <Card><CardHeader><CardTitle>Brazos A/B por episodio</CardTitle><p className="text-sm text-muted-foreground">El backend decide en ambos brazos: B solo puede bloquear más que A. Cero escapes en B no es un hallazgo del validador.</p></CardHeader><CardContent>
      <Table caption="Métricas por brazo" head={["Métrica", armText.A, armText.B]} rows={[
        ["Episodios · puntuados", `${e.arms.A.episodes} · ${e.arms.A.scoredEpisodes}`, `${e.arms.B.episodes} · ${e.arms.B.scoredEpisodes}`],
        ["Fallos de infraestructura", String(e.arms.A.infrastructureErrors), String(e.arms.B.infrastructureErrors)],
        ["Mezcla de casos puntuados", tally(e.arms.A.caseMix), tally(e.arms.B.caseMix)],
        ["Efectos simulados (sink ficticio)", String(e.arms.A.simulatedEffects), String(e.arms.B.simulatedEffects)],
        ["Escapes sobre casos inseguros", rate(e.arms.A.escapes), rate(e.arms.B.escapes)],
        ["Bloqueos falsos sobre admisibles", rate(e.arms.A.falseBlocks), rate(e.arms.B.falseBlocks)],
        ["Bloqueos backend sobre inválidos", String(e.arms.A.backendBlocksOnInvalid), String(e.arms.B.backendBlocksOnInvalid)],
        ["Latencia por episodio", latency(e.arms.A.episodeLatencyMs), latency(e.arms.B.episodeLatencyMs)],
      ]} />
    </CardContent></Card>
    <Card><CardHeader><CardTitle>{copy.title}</CardTitle><p className="text-sm text-muted-foreground">{copy.note}</p></CardHeader><CardContent><dl className="grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
      <Fact label="Revisiones del validador" value={`${v.reviewed} · ${tally(v.outcomes)}`} />
      <Fact label="Capturas sobre inválidos revisados" value={rate(v.captures)} />
      <Fact label="Capturas con los controles esperados" value={rate(v.capturesWithExpectedChecks)} />
      <Fact label="Capturas únicas (backend no las bloqueó)" value={String(v.uniqueCaptures)} />
      <Fact label="Omisiones contenidas por el backend" value={String(v.missesContainedByBackend)} />
      <Fact label="Rechazos falsos · rechazo o abstención sobre propuestas válidas al revisar" value={rate(v.falseRejections)} />
      <Fact label="Abstenciones" value={rate(v.abstentions)} />
      <Fact label="Salidas inválidas" value={rate(v.invalidOutputs)} />
      <Fact label="Timeouts" value={rate(v.timeouts)} />
    </dl></CardContent></Card>
    <Card><CardHeader><CardTitle>Pares A/B · McNemar exacto</CardTitle><p className="text-sm text-muted-foreground">Pares de la misma repetición; se excluyen {e.comparison.excludedPairs.incomplete} incompletos y {e.comparison.excludedPairs.infrastructure} con fallo de infraestructura.</p></CardHeader><CardContent>
      <Table caption="Tablas pareadas" head={["Subconjunto", "Pares", "Ambos admitidos", "Ambos bloqueados", "Solo B bloqueó", "Solo A bloqueó", "p exacto"]} rows={[
        pairRow("Todos", e.comparison.all), pairRow("Admisibles", e.comparison.admissible), pairRow("Inseguros", e.comparison.unsafe),
      ]} />
    </CardContent></Card>
    <Card><CardHeader><CardTitle>Costo y latencia del validador</CardTitle><p className="text-sm text-muted-foreground">{costBasisText[cost.basis]}.</p></CardHeader><CardContent><dl className="grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
      <Fact label="Llamadas LLM · proveedor · simuladas" value={`${cost.llmCalls} · ${cost.providerCalls} · ${cost.fakeCalls}`} />
      <Fact label="Llamadas por estado" value={tally(Object.fromEntries(Object.entries(cost.callsByStatus).filter(([, n]) => n > 0)))} />
      <Fact label="Sin usage conocido · pendientes" value={`${cost.callsWithUnknownUsage} · ${cost.pendingCalls}`} />
      <Fact label="Tokens entrada · salida" value={`${cost.tokens.input.toLocaleString("es-CO")} · ${cost.tokens.output.toLocaleString("es-CO")}`} />
      <Fact label="Tokens en caché · de razonamiento" value={`${cost.tokens.cachedInput.toLocaleString("es-CO")} · ${cost.tokens.reasoning.toLocaleString("es-CO")}`} />
      <Fact label="Costo (USD)" value={number(cost.costUsd, 6)} />
      <Fact label="Estimación previa (USD)" value={`${cost.estimateUsd.min}–${cost.estimateUsd.max}`} />
      <Fact label="Precios configurados (USD por Mtok)" value={`entrada ${cost.pricesUsdPerMtok.input} · salida ${cost.pricesUsdPerMtok.output}`} />
      <Fact label="Latencia por llamada" value={latency(cost.callLatencyMs)} />
    </dl></CardContent></Card>
    <Card><CardHeader><CardTitle>Desviaciones frente al oráculo</CardTitle><p className="text-sm text-muted-foreground">Las del validador son resultados experimentales; las del harness (brazo A o backend) nunca se esperan.</p></CardHeader><CardContent className="space-y-4">
      <dl className="grid gap-5 sm:grid-cols-3"><Fact label="Harness" value={String(e.deviations.harness)} /><Fact label="Infraestructura" value={String(e.deviations.infrastructure)} /><Fact label="Validador" value={String(e.deviations.validator)} /></dl>
      {e.deviations.harness > 0 && <p role="note" className="rounded-lg border border-amber-400 bg-amber-50 p-3 text-sm text-amber-950">El brazo A o el backend difieren del oráculo: revisar el harness antes de interpretar cualquier cifra de esta corrida.</p>}
    </CardContent></Card>
    <Card><CardHeader><CardTitle>Casos</CardTitle><p className="text-sm text-muted-foreground">{e.sampleRule}</p></CardHeader><CardContent className="space-y-3">
      {e.cases.map((c) => <details key={c.caseId} className="rounded-lg border p-3"><summary className="cursor-pointer text-sm font-medium">{c.caseId} · {caseKindText[c.caseKind]} · efectos simulados A {c.arms.A.effects}/{c.arms.A.episodes} · B {c.arms.B.effects}/{c.arms.B.episodes}</summary><div className="mt-3 space-y-4">
        <dl className="grid gap-3 sm:grid-cols-3"><Fact label="Resultados del validador (todas las repeticiones)" value={tally(c.validatorOutcomes)} /><Fact label="Fallos de infraestructura A · B" value={`${c.arms.A.infrastructure} · ${c.arms.B.infrastructure}`} /><Fact label="Par de la muestra" value={c.sample.pairId} /></dl>
        <div className="grid gap-3 md:grid-cols-2"><Episode arm="A" episode={c.sample.A} /><Episode arm="B" episode={c.sample.B} /></div>
        <Declarations experiment={e} item={c} />
      </div></details>)}
    </CardContent></Card>
    <Card><CardHeader><CardTitle>Fuente y límites</CardTitle></CardHeader><CardContent className="space-y-4">
      <ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">{e.notes.map((note) => <li key={note}>{note}</li>)}</ul>
      <dl className="grid gap-3 sm:grid-cols-3"><Fact label="SHA-256 de la implementación" value={e.implementationSha256} /><Fact label="SHA-256 de los casos" value={e.corpusSha256.cases} /><Fact label="SHA-256 del oráculo" value={e.corpusSha256.oracle} /></dl>
      {e.evidence.map((x) => <details key={x.artifact} className="rounded-lg border p-3"><summary className="cursor-pointer text-sm font-medium">{x.artifact} · ver procedencia</summary><dl className="mt-3 grid gap-3 sm:grid-cols-2"><Fact label="Puntero en artefacto" value={x.pointer} /><Fact label="SHA-256 del archivo original" value={x.sha256} /></dl></details>)}
      <p className="text-xs text-muted-foreground">{OPEN_GATES}</p>
    </CardContent></Card>
  </section>
}
