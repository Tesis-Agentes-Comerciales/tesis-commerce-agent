# POL-11 en D1 y política `b2-hp-v2`

Estado: implementado en `feat/d1-budget-rule` (ola «HP al 100 %», P1). Contrato de coordinación del
21-oct-2026, decisiones 1 y 2, y su adenda 1 (reglas por `(fase, purchase_mode)` y razones por id).

## Problema

En el piloto v1 el S02 quedó PARCIAL. El pedido es «Quiero exactamente el Google Pixel 8 de 128 GB
negro, pero no puedo gastar mas de 1.000.000 COP y no acepto otro modelo» y el Pixel 8 cuesta
1.540.000 en el catálogo. Cuando el modelo lee el millón como total, el filtro determinista solo
descarta por precio unitario, D1 (`b2-pilot-v1`: POL-01 y POL-02) permite, se crea el checkout y
`_enforce_authoritative_budget` lo cancela después. El comercio recibió un POST que la política
podía haber evitado: el precio observado ya superaba el presupuesto y el total del comercio nunca
es menor que el precio unitario por la cantidad.

## Alcance

- **POL-11, «Presupuesto total alcanzable».** D1, antes de crear el checkout. Deniega si
  `intent.max_total_minor` no es `null` y `selected_unit_minor × intent.quantity` lo supera.
  Código `BUDGET_NOT_SATISFIABLE`. Sin presupuesto total, cumple. Sin precio observado en COP
  (`selected_unit_minor` ausente), no cumple: la política no puede mostrar que el presupuesto
  alcanza (falla cerrado).
- **Política `b2-hp-v2`, revisión 1.** D1 evalúa POL-01, POL-02 y POL-11, en ese orden. P3 le
  agrega después las reglas de D2.
- **`demo-stack`** usa `b2-hp-v2` en los modos `b2` y `pol02-deny` (este con su lista excluyente).
- **UI:** la regla «Precio dentro de tu presupuesto» en la tabla de reglas de la traza y en la
  `PolicyBlockedCard`, con el precio del catálogo y el presupuesto total como montos.

Fuera de alcance: el presupuesto por unidad (lo cubre el filtro de la lista corta), D2, el runner
del piloto v2 (P4) y cambiar la guarda posterior. `_enforce_authoritative_budget` sigue igual: el
total autoritativo incluye envío e impuestos, que D1 no ve.

## Decisiones

1. **El piloto v1 sigue fijado a `b2-pilot-v1`.** `governance_pilot.py` lo pasa por defecto
   (`PILOT_V1_POLICY`). Sus resultados se publicaron con esa política y deben poder reproducirse:
   con POL-11, el S02 de B2 cambiaría de camino. El artefacto v1 conserva sus bytes y su digest:
   `policy_ids` sigue siendo `["POL-01","POL-02"]` y no se agregó ningún campo.
2. **Las reglas aplicables dependen de `(fase, purchase_mode)`.** `APPLICABLE_RULES` en
   `governance/models.py` es la tabla: hoy tiene una fila, `D1/HP` → POL-01, POL-02 y POL-11. P3
   agrega `D2/HP` → POL-03 a POL-06, y HNP agregará `D1/HNP`, `D2/HNP` y `D3/HNP` sin tocar las
   filas que ya existen, porque una fila es parte de los bytes de los artefactos armados con ella.
   `b2-pilot-v1` es anterior a la tabla y conserva su perfil fijo (POL-01 y POL-02).
3. **El conjunto de reglas sale del artefacto, y el artefacto, del `policy_set_id`.**
   `build_d1_policy` arma `b2-hp-v2` con la fila `D1/HP`; cualquier otro id conserva el perfil v1.
   Un artefacto en archivo (`AGENT_D1_POLICY_PATH`) solo puede declarar el perfil v1 o una fila D1
   de la tabla. El evaluador evalúa las reglas que lista el artefacto. La frontera
   (`D1PolicyBoundary`) deniega con `GOV_ARTIFACT_MISMATCH`, sin llamar al motor, si esas reglas
   no aplican a la fase y el modo de compra de la entrada (p. ej. `b2-hp-v2` con una entrada HNP),
   y con `GOV_OUTPUT_INVALID` una decisión cuyas reglas no son exactamente las del artefacto: un
   evaluador no puede saltarse POL-11.
4. **El hecho nuevo sale de la selección, nunca del modelo.** `selected_unit_minor` es el
   `observed_unit_amount_minor` de la variante elegida en el snapshot del feed que registró el
   vault (`feed_snapshot_ref`), en unidades menores de COP. El presupuesto y la cantidad ya
   estaban en `IntentFacts`, que salen de la intención validada. Nada de esto viene de un
   argumento del modelo.
5. **Compatibilidad de la evidencia.** `selected_unit_minor` es opcional y, ausente, no entra en la
   forma canónica: las decisiones guardadas antes de POL-11 se leen con el mismo digest y sus
   trazas siguen disponibles. Toda decisión nueva lo lleva cuando el feed está en COP.
6. **Reason codes por regla.** `PolicyDecision` acepta el perfil v1 o la fila de la tabla de su
   fase y modo de compra, y deriva cada código del id de la regla (`POLICY_REASONS`, POL-01 a
   POL-11), no de su posición. Un test fija el código de cada una.
7. **Montos en la traza.** `observed` y `expected` de una regla pasan a `string | Money | null`.
   POL-11 trae `{minor, currency}` y la UI los escribe con `formatMoney`, como el resto de los
   montos («Textos: se arman en la UI»). Las demás reglas no cambian.
8. **Chat y recuperación sin cambios.** El deny de POL-11 es un `d1_denied` como el de POL-02: el
   chat dice «La política de compra no permite esta compra. No se creó ningún checkout ni se hizo
   ningún cobro.» y la `PolicyBlockedCard` muestra la regla. La recuperación gobernada no
   interviene, porque no hubo checkout que cancelar.

## Contrato de datos

```text
SelectionPolicyInput.selected_unit_minor: Minor  # opcional; ausente = no observado en COP
APPLICABLE_RULES: {("D1", "HP"): ("POL-01", "POL-02", "POL-11")}
D1PolicyArtifact.policy_ids: ["POL-01","POL-02"] (b2-pilot-v1) | una fila D1 de APPLICABLE_RULES
PolicyId += "POL-11"; ReasonCode += "BUDGET_NOT_SATISFIABLE"
POLICY_REASONS: dict[PolicyId, str]  # POL-11 → BUDGET_NOT_SATISFIABLE
trace PolicyRule.observed / expected: Text | Money | None
```

POL-11 en la traza: `observed` = precio del catálogo (`selected_unit_minor`, COP), `expected` =
presupuesto total (`max_total_minor`, COP). Sin presupuesto total, `expected` es `null`.

| Regla | En la UI | Observado | Esperado |
| --- | --- | --- | --- |
| POL-11 | «Precio dentro de tu presupuesto» | «Precio en el catálogo» | «Tu presupuesto total» |

## Criterios de aceptación

- Con `b2-hp-v2` y el S02 leído como total, D1 deniega con `BUDGET_NOT_SATISFIABLE`, el comercio no
  recibe ningún `POST /checkout_sessions` y la traza muestra POL-11 en «No cumple» con 1.540.000 y
  1.000.000 COP.
- Con presupuesto total suficiente (S01: 1.600.000), POL-11 cumple y la compra sigue igual que
  antes; la guarda del total autoritativo sigue cancelando cuando el envío lo excede.
- `b2-pilot-v1` produce los mismos bytes y digest que antes, y su decisión sigue teniendo solo
  POL-01 y POL-02.
- `demo-stack` (`b2`, `pol02-deny`) exporta `AGENT_D1_POLICY_SET_ID=b2-hp-v2`; el runner del piloto
  sigue exportando `b2-pilot-v1`.
- Historias de `PolicyRuleTable`, `PolicyBlockedCard` y `ControlTimeline` con la regla, y un E2E de
  la tarjeta.

## Riesgos

- **El S02 del demo cambia de camino.** Leído como total, ya no crea y cancela un checkout: termina
  en la tarjeta de bloqueo de la política. El guion de la demo se actualiza en este PR.
- **P3 extiende el mismo artefacto y la tabla.** `b2-hp-v2` revisión 1 aún no tiene evidencia
  publicada, así que P3 puede cambiar sus bytes y agregar la fila `D2/HP`. Lo que no puede cambiar son los de `b2-pilot-v1`: cualquier campo
  nuevo de `D1PolicyArtifact` debe quedar fuera de la forma canónica cuando no aplica.
- **Una compra en curso al actualizar** con una entrada D1 guardada sin el hecho nuevo no se puede
  reanudar con la misma entrada y falla cerrado (`GOV_INPUT_INVALID`); su traza sigue legible.

## Plan de PR

Un PR: `feat(governance): add POL-11 budget rule to D1 and the b2-hp-v2 policy`.
