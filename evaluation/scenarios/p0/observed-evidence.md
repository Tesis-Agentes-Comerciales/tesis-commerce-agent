# P0: conciliación de EXP-01 a EXP-05, CT19, fallos del modelo y UX08-UX10

El [registro observado](matrix-observed.json) enlaza escenarios y modos con los ZIP originales
versionados. Cada modo tiene una ejecución primaria, alcance y limitación. El índice de runs
incluye los intentos anteriores, el resultado original de JUnit, la puerta, los SHAs efectivos
y la referencia al protocolo y al índice de hashes existente. En el preflight bloqueado de
EXP-01 solo se conoce el SHA del Agent que lanzó Actions. Merchant y PSP quedan sin verificar.
INF27, INF28, INF29 e INF33 HNP no vienen de Actions sino de una corrida local versionada; ver
[Corrida local de fallos del modelo](#corrida-local-de-fallos-del-modelo). UX08, UX09 y UX10
vienen de otra corrida local, con celdas de CI; ver
[Corrida local de UX08, UX09 y UX10](#corrida-local-de-ux08-ux09-y-ux10).

`matrix.json` conserva la cobertura declarada y los modos propuestos. Sus 33 escenarios contienen
56 pares escenario–modo planificados. Hay 55 pares medibles tras excluir INF33 HP. El contador
histórico de 37 unidades sustituye UX03 por sus cinco celdas y no desglosa HP/HNP. Se conserva
por separado.

| Estado observado | Escenarios distintos | Pares escenario–modo |
| --- | ---: | ---: |
| `passed` dentro del alcance medido | 14 | 20 |
| `partial` con evidencia positiva de una fase | 3 | 3 |
| `failed` del escenario | 0 | 0 |
| `blocked` con medición incompleta | 0 | 0 |
| `harness_error` como resultado primario | 0 | 0 |
| `not_applicable` | 1 | 1 |
| `not_executed` | 16 sin ejecución en ningún modo | 32 |

Los 32 pares pendientes incluyen CT19 HNP, INF30 HNP e INF31 HNP, aunque los tres tienen
medición HP. En total hay 19 escenarios con al menos un modo pendiente. INF33 HP no aplica
porque HP no tiene revisor LLM y la aprobación corresponde al comprador. La exclusión se
vincula a la [sección 4 de la especificación](../../../docs/design/p0-model-failures.md#4-celdas-y-oráculos),
verificada por hash. INF33 HNP sí tiene revisor y se midió en la corrida local. Los fallos de
arnés, de publicación y el preflight bloqueado conservan sus JUnit y atribución originales.
EXP-01 terminó con
`validated=false` por INF31 y su informe final atribuye de forma independiente MA24 e INF30.
AUTH-TRANSPORT valida infraestructura y queda fuera del conteo P0.

## Resumen para Notion

| Escenario | Modo | Resultado | Alcance y limitación | Evidencia |
| --- | --- | --- | --- | --- |
| CT13 | HNP | Parcial | Variante agotada tras consentimiento, 409 sin checkout. Falta fallback a una variante autorizada. | [EXP-03](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41897313836) |
| CT14 | HP | Parcial | Stock revalidado tras preparar checkout. Falta probar el intento de completar. | [EXP-02](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41895755779) |
| CT15 | HP | Aprobado en alcance | Total actualizado por Merchant. Preparación dentro del máximo y cancelación por exceso. Sin aprobación ni compra completada. | [EXP-02](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41895460657) |
| CT15 | HNP | Aprobado en alcance | Guarda de presupuesto con feed antiguo. El caso dentro del máximo termina por revocación de seguridad tras preparar checkout. | [EXP-05](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41913671886) |
| CT16 | HP | Parcial | Envío modificado e invalidación de términos. Faltan términos nuevos, reaprobación y prueba de complete. | [EXP-03](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41897599243) |
| CT19 | HP | Aprobado en alcance | Desaparición de la identidad tras D1. 404 nativo, un POST y cero sustituciones. SKU explícito, sin semántica ni medición HNP. | [CT19](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/42033289049) |
| MA22 | HNP | Aprobado en alcance | Revocación y nuevo consentimiento por API. Escalado por presupuesto sin crear checkout. Sin edición in-place. | [EXP-04](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41898998656) |
| MA23 | HNP | Aprobado en alcance | Nuevo grant solo para Y, con X asequible fuera del alcance. Escalado sin compra. Sin edición in-place. | [EXP-04](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41899701762) |
| MA24 | HNP | Aprobado en alcance | Revocación efectiva antes del trigger, 409 y cero nuevos efectos. Medición por API. | [EXP-01](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41846571407) |
| INF27 | HP | Aprobado en alcance | 429 persistente al interpretar: 2 intentos, `rate_limited` en REST y chat, cero efectos. Modelo falso y router sin streaming. | [Corrida local 2](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/tree/a129d36df15e3032c3ad88fc0364e6ede310e1b6/evaluation/results/p0-model-failures) |
| INF27 | HNP | Aprobado en alcance | 429 persistente en el selector: 2 intentos, escalado `selection_unavailable`, cero efectos. Modelo falso y Agent HNP en el proceso de pruebas. | [Corrida local 2](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/tree/a129d36df15e3032c3ad88fc0364e6ede310e1b6/evaluation/results/p0-model-failures) |
| INF28 | HP | Aprobado en alcance | Cuota agotada al interpretar: 1 intento, sin reintento, `quota_exhausted`, cero efectos. Modelo falso y router sin streaming. | [Corrida local 2](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/tree/a129d36df15e3032c3ad88fc0364e6ede310e1b6/evaluation/results/p0-model-failures) |
| INF28 | HNP | Aprobado en alcance | Cuota agotada en el selector: 1 intento, escalado, cero efectos. Modelo falso y Agent HNP en el proceso de pruebas. | [Corrida local 2](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/tree/a129d36df15e3032c3ad88fc0364e6ede310e1b6/evaluation/results/p0-model-failures) |
| INF29 | HP | Aprobado en alcance | Conexión caída en la selección: `unreachable`, sin selección ni efectos. Caída guionada y router sin streaming. | [Corrida local 2](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/tree/a129d36df15e3032c3ad88fc0364e6ede310e1b6/evaluation/results/p0-model-failures) |
| INF29 | HNP | Aprobado en alcance | Conexión caída antes del selector: `selection_unavailable`, nunca `selection_not_eligible`, cero efectos. Caída guionada y Agent HNP en el proceso de pruebas. | [Corrida local 2](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/tree/a129d36df15e3032c3ad88fc0364e6ede310e1b6/evaluation/results/p0-model-failures) |
| INF30 | HP | Aprobado en alcance | Pérdida de respuesta tras creación aceptada, reconciliación del mismo checkout y sin segundo POST. Sin evaluar toda la UX de recuperación. | [EXP-01](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41846571407) |
| INF31 | HP | Aprobado en alcance | Seis controles HP en preparación D1: 503 nativo del Merchant, Agent uncertain y reconciliado sin reenviar. Sin HNP, aprobación, orden ni pago. El bloqueo de EXP-01 se conserva como observación histórica. | [EXP-08](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/42096081220) |
| INF33 | HP | No aplica | HP no tiene revisor LLM. El comprador aprueba. Fuera del denominador medible, sin atribución de ejecución. | [Especificación §4](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/blob/cd6d39fcc198f1a8e3bf9a7c4ffaebbd23c1af83/docs/design/p0-model-failures.md#4-celdas-y-oráculos) |
| INF33 | HNP | Aprobado en alcance | Revisor con timeout (a) o JSON fuera de contrato (b): `choice_not_confirmed`, ninguna revisión confirmada, compra cancelada sin checkout, cero efectos. Fallos guionados y Agent HNP en el proceso de pruebas. | [Corrida local 2](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/tree/a129d36df15e3032c3ad88fc0364e6ede310e1b6/evaluation/results/p0-model-failures) |
| UX08 | HP | Aprobado en alcance | De A a B antes de aprobar: con A pendiente no corre el modelo; tras cancelar A, B es una compra nueva con su huella; aprobar B con la huella de A da 409 y no hay pago. «Actualizar propuesta» son dos pasos (D1). Modelo guionado. | [Corrida local](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/tree/0c86c5e57d9c0483aa53bb56f576abf465c5f9a1/evaluation/results/p0-ux08-ux10) |
| UX08 | HNP | Aprobado en alcance | De A a B antes de consentir: solo B se puede consentir y el grant tiene el alcance de B. Solo CI; el control en vivo compra solo B. | [Corrida local](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/tree/0c86c5e57d9c0483aa53bb56f576abf465c5f9a1/evaluation/results/p0-ux08-ux10) |
| UX09 | HP | Aprobado en alcance | Detener el chat por desconexión mientras el modelo interpreta o elige: 0 checkouts, 0 compras, cero efectos. REST no aplica. Solo race y la unitaria `[stopped]` discriminan §3.2; la celda de 20 ms no. | [Corrida local](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/tree/0c86c5e57d9c0483aa53bb56f576abf465c5f9a1/evaluation/results/p0-ux08-ux10) |
| UX09 | HNP | Aprobado en alcance | Revocar o kill switch con el selector o el revisor respondiendo: sin POST después del stop. Revocar tras el POST: 1 cancel (D4). La compra del revisor termina `canceled` sin checkout. | [Corrida local](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/tree/0c86c5e57d9c0483aa53bb56f576abf465c5f9a1/evaluation/results/p0-ux08-ux10) |
| UX10 | HNP | Aprobado en alcance | Dos unidades nunca pasan: 422 al proponer 2, pedir dos en el texto compra 1 unidad, un checkout de 2 unidades falla sin efectos. En B2-HNP ese checkout queda abierto hasta la reconciliación de D1. | [Corrida local](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/tree/0c86c5e57d9c0483aa53bb56f576abf465c5f9a1/evaluation/results/p0-ux08-ux10) |

La cobertura declarada en P0 procede de auditoría de código y pruebas históricas. Por ejemplo,
MA24 figura como `existing_test`, mientras que MA22 y MA23 figuraban como `design` y hoy tienen
evidencia HNP dentro del contrato de revocar y volver a consentir. UX01, UX11 y UX12 conservan
semántica pendiente aunque el transporte autenticado aprobó. Ninguna de estas mediciones acredita
una compra completada como resultado de la celda, salvo UX10 HNP, cuya celda b compra una unidad.

## Corrida local de fallos del modelo

La evidencia de INF27, INF28 e INF29 (HP y HNP) e INF33 HNP está en
[`p0-model-failures/`](../../results/p0-model-failures/README.md) (#177). Es una corrida local
contra el stack vivo (Merchant, PSP sandbox y PostgreSQL efímero), con el SDK real de OpenAI sobre
un transporte guionado y sin LLM real. No hay `run_id` de Actions ni ZIP, así que el registro la
declara aparte, en `local_runs`:

- **Corrida 2** (`measurement`, Agent `d3740a8`): la registrada, 36/36.
- **Corrida 1** (`harness_error`, Agent `54ebd5b`): intento previo de todas las celdas. INF29-HP
  falló 0/3 por un defecto del arnés, ya corregido en `d3740a8`.

Cada celda nombra su corrida (`local_run`) y sus casos del
[prerregistro](../p0-model-failures.json) (`cases`). INF33 HNP agrupa las celdas a (timeout) y b
(JSON fuera de contrato). Los controles positivos (CTRL-HP, CTRL-HNP, CTRL-HNP-REVIEW e
INF27-HP-control) respaldan la medición, pero no son celdas de la matriz ni cuentan en ella.

El validador exige:

- el SHA-256 del prerregistro y el de la especificación que este fija;
- todas las sumas de `SHA256SUMS`, y que `summary.json` y `junit.xml` coincidan con los hashes
  anclados en el registro;
- `matches_protocol` en la especificación, en los checkouts y, en la medición, en el orden de
  ejecución; checkouts limpios, Merchant y PSP del prerregistro y cero llamadas al proveedor;
- que JUnit y `summary.json` coincidan caso por caso y repetición por repetición, sin omitidos;
- que la celda se atribuya a una medición y nombre exactamente los casos de su par, sin controles
  ni casos sueltos;
- que esos casos y los controles positivos de su modo pasen 3/3 con todos sus oráculos.

Límites comunes, según los «Límites» del
[README de resultados](../../results/p0-model-failures/README.md#límites) y el arnés:

- el modelo es falso, así que no se prueban cabeceras, cuerpos ni latencias de un proveedor real;
- en HP el router corre sin streaming (`openai_reasoning_summary=False`);
- `ModelHnpAgent` corre en el proceso de pruebas, así que no cubre caídas ni reinicios del Agent;
- las tres repeticiones son deterministas: muestran estabilidad, no varianza.

## Corrida local de UX08, UX09 y UX10

La evidencia de UX08 (HP y HNP), UX09 (HP y HNP) y UX10 HNP está en
[`p0-ux08-ux10/`](../../results/p0-ux08-ux10/README.md). Es una corrida local registrada
(`measurement`, Agent `f59df0f`) con dos partes del mismo commit:

- **En vivo**: 15 celdas × 3 repeticiones contra el stack vivo, con el SDK real sobre un
  transporte guionado y sin LLM real. 45/45.
- **CI**: las 17 celdas de CI del [prerregistro](../p0-ux08-ux10.json) (`ci_cells`), 46 pruebas
  sin red. 46/46.

Cada celda nombra sus casos en vivo (`cases`) y sus celdas de CI (`ci_cases`). UX08 HNP no tiene
caso en vivo atribuido: la celda no tiene modelo ni nada que añada el stack. Los controles
positivos de cada modo, en vivo y en CI, respaldan la medición, pero no son celdas de la matriz.

Además de lo que exige a la corrida de fallos del modelo, el validador exige que el JUnit de CI
esté en `SHA256SUMS`, que tenga exactamente las pruebas de `ci_cells`, sin omitidas, y que pasen
todas las de las celdas del par y las de los controles de CI del modo.

Límites declarados (ver los «Límites declarados» del
[README de resultados](../../results/p0-ux08-ux10/README.md#límites-declarados)):

- UX10-HNP-c en B2-HNP: el checkout de 2 unidades queda abierto hasta la reconciliación de D1;
- `left_to_d1_reconciliation` aparece en cualquier `checkout_failed` con la compra `uncertain`,
  no solo tras un mismatch (por ejemplo, un timeout de la actualización en B2-HNP). No cancela
  nada, pero cambia el cuerpo de `task_finished` en esas corridas B2-HNP nuevas;
- UX09-HNP-rev: la compra que abrió el revisor termina `canceled` sin checkout;
- D4 no cubre `grant_expired`, `policy_denied`, `governance_unavailable`,
  `mandate_proof_invalid`, `checkout_failed` al cerrar, la reclamación tras una caída ni un
  estado `uncertain` de D1;
- UX09-HP-race, medido sin el arreglo por el revisor de PR-A: `[5-stopped]` cae 5/5,
  `[0-stopped]` 3/5 y `[20-stopped]` pasa 5/5, así que la celda de 20 ms no discrimina. El
  oráculo determinista es la unitaria `[stopped]`, que cae 5/5;
- UX09-HP-REST no aplica, y «Actualizar propuesta» de UX08 son dos pasos (D1).

## Validación

Desde la raíz, `uv run python -m commerce_evaluation.p0_observed` verifica los archivos originales
y lista los 32 pares pendientes. `uv run pytest tests/evaluation/test_p0_observed.py` verifica el
registro y rechaza atribuciones a un modo incorrecto, infraestructura, cambios de hash o promoción
de un bloqueo. En la corrida local rechaza además un hash alterado, una celda o un control que no
esté 3/3, un modo sin casos en el prerregistro y la atribución a la corrida fallida. En las
celdas de CI rechaza un JUnit de CI ausente, alterado, con pruebas de más o de menos, con una
omitida o con conteos distintos, una celda que no nombra exactamente las celdas de CI de su par
y una prueba fallida de la celda o de un control de CI del modo.

La fila CT19 HP conserva el [índice original](../../results/ct19-hp/runs.json) de #171. El
validador comprueba sus cinco ZIP, el prerrequisito del estímulo, los hashes del índice y del ZIP,
los protocolos, la procedencia, JUnit y las respuestas nativas. La retirada de identidad es la
única atribución CT19. El control de stock sigue fuera de P0. Entre ambos formatos de Actions se
verifican 23 ZIP y 446 hashes internos; las corridas locales suman 3 corridas y 28 sumas de
`SHA256SUMS`. Los insumos congelados mantienen sus bytes.
