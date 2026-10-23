# Elección restringida a la lista corta y número de orden visible

Estado: **implementado** en #65 (puntos 1 y 2) y #66 (punto 3) · 20 de octubre de 2026 · ola
«HP al 100 %», P5b.

## Problema

En el ensayo en vivo del 20 de octubre, S02 («Quiero exactamente el Google Pixel 8 de 128 GB
negro, pero no puedo gastar mas de 1.000.000 COP y no acepto otro modelo») terminó con «La opción
seleccionada no está disponible en el catálogo del comercio», mientras la UI mostraba el Pixel 8
disponible en la lista corta. Pasó esto:

1. El modelo leyó el millón como presupuesto total. El filtro determinista solo descarta por
   precio unitario, así que el Pixel 8 (1.540.000 COP) quedó como único candidato.
2. Con un solo candidato que cumple todas las preferencias, el router usa `CandidateChoice`:
   `action` forzada a `purchase` y `selected_variant_id` como texto libre.
3. El modelo devolvió un id que no estaba en la lista corta. El router lanzó
   `UnknownVariantSelection` y la recuperación gobernada respondió con `cause: not_in_catalog`.
4. El id devuelto no quedó registrado, así que no se puede saber qué devolvió el modelo.

Además, al completar una compra el mensaje final del agente dice `ord_<uuid>` y el recibo dice
`TST-XXXX`.

## Alcance

1. **Elección restringida.** Los schemas de salida estructurada de la elección (`CandidateChoice`
   y `CandidateDecision`) limitan `selected_variant_id` a los ids de la lista corta con un `enum`
   generado en cada llamada.
2. **Id rechazado registrado.** Si aun así llega un id fuera de la lista corta, el registro
   `governed_recovery` de `hp_evidence` guarda su sha256, nunca el texto crudo. El texto canónico
   de `not_in_catalog` deja de contradecir la UI, y la etiqueta de esa causa en la traza también.
3. **Número de orden visible.** El mensaje final tras completar usa el número que muestra el
   recibo, con la misma fuente.

Queda **fuera**: cambiar el filtro determinista de presupuesto (POL-11 en D1 es de P1), cambiar
las instrucciones del modelo, cambiar los datos o resultados del piloto v1 y agregar el hash al
JSONL del piloto (`HP_RESULT_FIELDS` sigue con `control`, `kind` y `cause`; el hash queda en
`agent.db`, como los demás digests).

## Diseño

### 1. `enum` por llamada

El router usa `client.responses.parse(text_format=<modelo Pydantic>)`. El SDK de OpenAI convierte
el modelo a un JSON Schema estricto (`strict: true`) y la API decodifica con ese schema. Structured
Outputs admite `enum` en modo estricto, con hasta 1000 valores por schema; la lista corta tiene
como máximo `MAX_MODEL_CANDIDATES = 10` ids de hasta 256 caracteres.

En cada llamada, `choose_candidate` crea con `pydantic.create_model` una subclase del modelo de
respuesta con el mismo nombre (`CandidateChoice` o `CandidateDecision`), donde
`selected_variant_id` lleva `WithJsonSchema({"type": "string", "enum": [ids]})`. En
`CandidateDecision` el campo sigue siendo anulable (`anyOf` con `null`) para `clarify`.
`CandidateClarification` no elige variante y no cambia.

El `enum` solo cambia el schema que se envía al modelo. La validación de Pydantic sigue aceptando
cualquier texto, así que un id que llegue fuera de la lista corta (otro router, un cambio de
proveedor o un fallo de la decodificación) no se pierde en un `ValidationError` genérico: sigue
llegando a `UnknownVariantSelection` y a la recuperación gobernada, que lo registra.

### Decisión: `CandidateChoice` no permite declinar ni aclarar

Cuando el único candidato contradice una restricción dura conocida (por ejemplo, su precio unitario
ya supera el presupuesto total), `CandidateChoice` **sigue forzando `purchase`**. Razones:

- **Las restricciones duras son deterministas.** La lista corta llega al modelo después de los
  filtros de dominio. El presupuesto total lo hacen cumplir POL-11 en D1 antes de crear el checkout
  (B2 con `b2-hp-v2`, P1) y la guarda común del total autoritativo después de crearlo (B0' y B2).
  Dar al modelo la opción de declinar movería una regla dura al modelo, sin determinismo ni
  evidencia de política.
- **Atribución en el piloto v2.** Si el modelo pudiera declinar, B0' mejoraría S02 por
  discreción del modelo y POL-11 casi no actuaría en B2. La diferencia entre condiciones dejaría
  de medir la gobernanza.
- **Coherencia con las instrucciones.** `_SELECTION_INSTRUCTIONS` ya pide no aclarar por el total
  y deja el chequeo al backend.
- **El fallo observado desaparece.** El modelo escapó de la elección forzada inventando un id. Con
  el `enum`, la única salida válida es el id de la lista corta.

### 2. Hash del id rechazado y texto de `not_in_catalog`

`GovernedRecoveryRecord` suma `selected_variant_sha256: str | None`. Es el sha256 hexadecimal del
id tal como llegó y se llena en todo `selection_outside_shortlist`, conozca o no el catálogo ese
id. En `no_candidate` y `budget_exceeded_after_checkout` es `null`. El texto crudo nunca entra en
la evidencia: con `not_in_catalog`, `variant_id` sigue en `null`.

Texto canónico nuevo (`CAUSE_TEXT["not_in_catalog"]`), seguido como siempre del cierre común:

> El modelo propuso una opción que no está entre las verificadas para tu pedido. No preparé
> ningún checkout ni se hizo ningún cobro. Si quieres, ajusta tu pedido o tu presupuesto y lo
> busco de nuevo.

En la traza, `RECOVERY_CAUSE.not_in_catalog` pasa a «El modelo propuso una opción que no estaba
entre las verificadas para tu pedido», en paralelo con `not_shortlisted`. El contrato de la traza
no cambia: el hash no viaja a la UI.

### 3. Número de orden visible

El recibo de la tarjeta muestra `order.order_number ?? order.id` de la respuesta de `/complete`,
que es el snapshot guardado en `completion_results`. El mensaje de `purchase_update` con
`stage = completed` lee ese mismo snapshot: usa `order_number` y, si el comercio no lo envía, el id
de la orden. La vista `purchase` (`run_id`, `stage`, `order_id`) no cambia, así que el contrato de
Assistant Transport tampoco cambia; solo cambia el texto.

## Efecto esperado en S02

| Lectura del presupuesto | Antes | Después |
| --- | --- | --- |
| Total (un candidato, el Pixel 8) | El modelo puede inventar un id: B0' responde 502 y B2 explica `not_in_catalog` | El modelo solo puede elegir el Pixel 8. B2 con `b2-hp-v2`: D1 deniega con `BUDGET_NOT_SATISFIABLE` antes del checkout (P1). B2 con `b2-pilot-v1`: checkout creado y cancelado (`budget_exceeded_after_checkout`). B0': checkout creado y cancelado, 409 `budget_exceeded` |
| Unitario (lista corta vacía) | `no_candidate` | Sin cambio |

`not_in_catalog` queda como defensa: con el router de OpenAI ya no debería aparecer, y si aparece
queda el hash del id para investigarlo.

**El router lo comparten B0' y B2**, así que este cambio modifica ambas condiciones. El piloto v2
las vuelve a correr; los resultados del piloto v1 no se tocan.

## Criterios de aceptación

- El `text_format` de `CandidateChoice` y `CandidateDecision` tiene, en su schema estricto, un
  `enum` con exactamente los ids de la lista corta que recibió el modelo, en orden y sin repetir.
- Un id fuera de la lista corta sigue lanzando `UnknownVariantSelection` (B0' conserva su 502).
- El registro `governed_recovery` de un `selection_outside_shortlist` guarda
  `selected_variant_sha256 = sha256(id)`, y el id crudo no aparece en el cuerpo cuando el catálogo
  no lo conoce.
- El texto de `not_in_catalog` no dice que la opción no está en el catálogo del comercio.
- Tras completar, el mensaje dice el `order_number` del comercio, o el id si no hay
  `order_number`.

## Plan de PRs

1. `fix/selection-shortlist-enum`: puntos 1 y 2, con esta spec.
2. `fix/visible-order-number`: punto 3, con esta misma spec (archivo idéntico).

## Riesgos

- **Un id malformado igual puede llegar** si el proveedor no aplica el schema estricto. Lo cubren
  `UnknownVariantSelection` y el hash.
- **Más checkouts creados en B0'.** Donde antes había un 502 sin efecto, ahora puede haber un
  checkout creado y cancelado. Es el comportamiento de B0 con una elección válida y el piloto v2
  lo mide así.
