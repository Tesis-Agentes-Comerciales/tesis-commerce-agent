# EXP-08 · Reevaluación INF31 HP

**Resultado observado · `passed` con alcance acotado.** Los seis controles
se ejecutaron en Agent–Merchant–PSP. JUnit registró 6 aprobados y 0 fallos,
errores u omisiones. El reporter registró `validated=true`, `issues=[]`,
y 0 `xfail`, `xpass`, `blocked` y `not_executed`. El escenario se atribuye
una vez, a INF31 HP en la frontera de preparación D1 con un SKU observado.
[Run 42096081220](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/42096081220).

## Requisito, prueba y resultado

| Requisito | Control o prueba | Resultado observado | Evidencia |
| --- | --- | --- | --- |
| 503 inyectado antes de reenviar | `proxy_503` | Una solicitud al proxy y cero reenvíos. Agent `uncertain` y cero checkouts. | JUnit y observación del control en el ZIP |
| Timeout antes de reenviar | `timeout_before_forward` | Se retuvo el POST en el proxy hasta que el Agent agotó su espera. El 503 local posterior no llegó al Agent a tiempo. Cero reenvíos y cero checkouts. | JUnit y observación del control en el ZIP |
| 503 originado en Merchant | `merchant_503` | PostgreSQL operativo al preparar, detenido después de comprobar la persistencia de D1 y antes de reenviar. Exactamente un POST reenviado y respondido por Merchant. 503 con `lab_session_store_unavailable` y `Request-Id` de servidor. PostgreSQL restaurado. | JUnit, trazas y observaciones de proxy y base en el ZIP |
| Incertidumbre y no reenvío ciego | Los cuatro controles de incertidumbre | Un solo POST por control. Un segundo intento local exigió reconciliar. En las tres creaciones no observadas, `not_found` conservó `NOT_FOUND_NOT_ABSENCE`. | Estados del Agent, conteos y resultados de lookup en el ZIP |
| Respuesta perdida tras aceptación | `timeout_after_accept` | Merchant respondió 201 y el Agent agotó la espera. La reconciliación verificó el checkout histórico y el digest original. El checkout quedó cancelado. | JUnit, digest de la respuesta y snapshot de cleanup en el ZIP |
| Rechazo comercial definitivo | `definitive_stock_refusal` | La mutación cambió disponibilidad, stock y su revisión. Merchant rechazó con 409 `out_of_stock`, el Agent quedó `failed`, se crearon cero checkouts y se restauró el snapshot exacto de la oferta. | JUnit y snapshots `offer_at_dispatch` y `offer_restored` |
| Preparación positiva | `positive` | Merchant respondió 201. El Agent quedó `awaiting_confirmation` y se canceló el único checkout preparado. | JUnit, observaciones Merchant y cleanup |
| Independencia, efectos y limpieza | Lecturas Merchant, Agent y PSP en los seis controles | Cero aprobaciones, órdenes, intentos de completar y delegaciones PSP. Los snapshots de PSP permanecieron idénticos. Durante la caída no apareció una fila de auditoría y la traza registra el intento fallido. Los dos checkouts aceptados quedaron cancelados. Proxy cerrado y PostgreSQL retirado. | Snapshots independientes, `merchant.log`, `exp01-report.json` y `evidence.jsonl` |

Los resultados de la tabla pertenecen a Agent
`3e724f2ed7c33b4f9f167181cdc1c1943e6324f3`, Merchant
`5db4bd5da814fc621be7b1caf067eaa88e6a1a67` y PSP
`e6ab67b5963434a9a3caae2012b70232a3cca65d`. El manifiesto confirma los tres
árboles limpios. La base Agent fue `bf54850d3862083ab36705608de2b8e5a883f2c3`.
[CI de la base Agent](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/42093445906).

## Integridad y custodia

- [Protocolo congelado](../../scenarios/exp08-inf31-hp.json), SHA-256
  `bfe8a97bd5f49949c378528f05ffa563c027ce8fe10aa6eae818a622723d5c8b`.
  Cada episodio contiene su oferta observada, catálogo completo y tiempos de
  congelación anteriores a la observación. Los controles usan el reloj del arnés.
- [ZIP original](runs/42096081220.zip), 373 245 bytes, SHA-256
  `121b8d9a09144d0bbc115365cc724d9a2429d042742dae6f15be5a3d39f5c245`.
  Coincide con el digest de GitHub y con [SHA256SUMS](SHA256SUMS).
  Se verificaron 38 hashes internos y 34 copias idénticas bajo alias
  `*current` del runner. El ZIP contiene trazas sanitizadas y se conserva
  versionado porque el artefacto de Actions vence el 19 de noviembre de 2026 UTC.
  [Artefacto 13447358096](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/42096081220/artifacts/13447358096).
- JUnit `fa5aa3070cb34a84263c3b2e9075cc8f5b8d7cb492c4009b759c48a9efd70968`,
  reporter `9ab7d48140b4b97888a8f5d03854b08f839333461dc6752b1c46d82ec4a00568`,
  manifiesto `3878da07b1f899aab75583000814293e40d5c1aa90b2c1997815c1745ad9fe07`.
  Las seis identidades de JUnit coinciden con el protocolo y los registros.

## Atribución y dependencia

El intento original EXP-01 conserva `blocked` con 500 nativo, 7 pruebas
aprobadas y 1 fallida. Su evidencia y sus cuatro intentos previos siguen
referenciados. La observación nueva se registra en
[`matrix-observed.json`](../../scenarios/p0/matrix-observed.json), conforme a
la separación del prerregistro. El registro deriva 33 escenarios, 56 pares
planificados y 55 medibles, con 15 aprobados, 3 parciales, 37 sin ejecutar
y 1 no aplicable. El alcance excluye HNP, interfaz, conversación, compras
y pagos, y se limita a la negativa TCP medida en la apertura de la conexión.
[EXP-01 histórico](../exp-01-first-batch/final/README.md).

La integración de la reevaluación requiere aprobar e integrar primero la
corrección de Merchant. El perfil manual `exp08-inf31-hp` reutiliza la
prevalidación de pines y el reporter. `full`, la programación y la compuerta
específica de P2 conservan sus políticas.
[Merchant #12](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-merchant/pull/12).
