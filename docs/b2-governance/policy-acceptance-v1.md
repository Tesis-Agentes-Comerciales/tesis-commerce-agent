# Aceptación del contrato de políticas B2 v1

Estado: **PROPUESTO PARA REVISIÓN**, EC-GOV-01, 2026-10-14.
Todas las pruebas de este documento están **DISEÑADAS, NO EJECUTADAS**.
No son resultados B2 ni una validación AP2/HNP. Fuente normativa propuesta:
[policy-v1.md](policy-v1.md); motor candidato y gate D11 en el
[ADR](adr-policy-engine-v1.md). Trazabilidad original:
[roadmap](system-design-v1/roadmap.md), [decisiones](system-design-v1/decisions.json),
[amenazas](threat-model.md), [evaluación anterior](evaluation-plan.md),
[matriz piloto histórica](../../evaluation/data/pilot-matrix.md) y
[escenarios congelados](../../evaluation/data/pilot-scenarios.json).

## 1. Oráculos, fixture de referencia y observación

El oráculo se fija por caso antes de ejecutar; el motor no etiqueta su propio éxito.
Usar entradas sintéticas estrictas, artefacto fijado y reloj explícito. Para P01–P10:
run/owner/intención/feed/selección coherentes; merchant `merchant-1`, variante `variant-1`,
COP, cantidad 1, unit=89.000.000, shipping=1.000.000, other=0 y total=90.000.000
en **unidades menores**. Intención: max_unit=90.000.000 y max_total=100.000.000.
No son cotizaciones reales ni fixtures B0 modificadas.

En fixtures HNP: grant de una compra activa, mismo merchant/variante/destino/instrumento,
total_max=100.000.000, cero usos consumidos/reservas ajenas; authority_now dentro del
intervalo. D2 parte sin reserva o con reserva propia idéntica; D3 parte con reserva
propia `reserved` y prueba cerrada válida según fixture. `valid` es un hecho simulado
en pruebas puras del PDP, no evidencia de que el puente AP2 funcione. Las pruebas
criptográficas reales son un gate separado H01/H03.

Cada caso P/N modifica solo los campos enumerados del fixture correspondiente;
resto de hechos/perfiles/prerrequisitos válidos. Matriz parametrizada:
D1 en HP y HNP; POL-03–06 en ambas ramas D2; POL-07/08 solo D2-HNP;
POL-09 en D2-HNP y D3-HNP; POL-10 solo D3-HNP. No presentar una política
no aplicable como pass. `off/shadow/enforce` se ensayan por separado.

Oráculo de política: outcome, lista exacta de razones, rule_results aplicables,
input_digest y binding del artefacto. Oráculo de aplicación en enforce:
contadores independientes de create, firma/cierre, token/delegación de pago,
despacho, reserva y transición. Un texto de UI o un ID generado por el runner no
demuestra ausencia/presencia de efecto. Una prueba pura solo acredita el PDP;
la prueba de servicios debe observar los gateways y el store efectivamente usados.

Evidencia `EVAL`: entrada canónica completa sin secretos, artefacto, decisión,
fuentes y refs reconstruibles. Evidencia `APPLY`: EVAL + versiones y resultado
de UoW/CAS, operación y efectos observados. En fallos previos al parseo usar
la variante `input_rejected`, nunca guardar payload inválido arbitrario.

## 2. Matriz por política: positivos, negativos y fail-closed

P = permiso con el fixture válido; N = rechazo normativo; F = fallo o dato
incompleto, siempre sin efecto protegido en enforce. Razones GOV son del wrapper/
aplicación y no se cuentan como incumplimientos de una POL no evaluada.

| Caso | Política / punto | Variación o inyección | Resultado exacto esperado | Efecto/evidencia exigidos |
| --- | --- | --- | --- | --- |
| P01 | POL-01 / D1 | Elegido dentro de elegibles del mismo intent/feed | allow; ALL_APPLICABLE_POLICIES_SATISFIED | EVAL; solo podría crear tras guardas y persistencia |
| N01 | POL-01 / D1 | Elegido válido como ID, ausente de elegibles | deny; SELECTION_NOT_ELIGIBLE | create=0; EVAL con conjunto completo |
| F01 | POL-01 / D1 | Falta eligible_variant_ids o duplicado prohibido | rechazo GOV_INPUT_INVALID antes de motor | create=0; input_rejected; no allow fabricado |
| P02 | POL-02 / D1 | Acción y merchant configurados | allow; ALL_APPLICABLE_POLICIES_SATISFIED | EVAL incluye perfil por digest |
| N02 | POL-02 / D1 | Merchant distinto, ID sintácticamente válido | deny; ACTION_OR_MERCHANT_NOT_ALLOWED | create=0; no request al merchant ajeno |
| F02 | POL-02 / D1 | Artefacto de allowlist no recuperable/verificable | GOV_ARTIFACT_MISMATCH o GOV_FACTS_UNAVAILABLE según frontera inyectada | create=0; resultado operativo, no POL-02 pass |
| P03 | POL-03 / D2 | Principal autenticado, owner correcto y acción permitida | allow; ALL_APPLICABLE_POLICIES_SATISFIED | EVAL con refs, sin JWT; todavía no firma sin guardas |
| N03 | POL-03 / D2 | principal.permitted_actions excluye complete_purchase | deny; PRINCIPAL_ACTION_NOT_ALLOWED | firma/cierre=0; EVAL |
| F03 | POL-03 / D2 | Backend no puede verificar identidad/ownership | rechazo de auth o GOV_FACTS_UNAVAILABLE, según entrada fallida | firma/cierre=0; no enumerar compras ajenas |
| P04 | POL-04 / D2 | D1 y presentación HP o grant/operación HNP vinculados | allow; ALL_APPLICABLE_POLICIES_SATISFIED | EVAL con relaciones resueltas |
| N04 | POL-04 / D2 | D1 resuelto pertenece a otro run/intent | deny; EPISODE_BINDING_MISMATCH | firma/cierre=0; EVAL identifica vínculo incorrecto |
| F04 | POL-04 / D2 | Solo llega ID de D1 y no puede resolverse su evidencia | GOV_FACTS_UNAVAILABLE | firma/cierre=0; no asumir allow por ID |
| P05 | POL-05 / D2 | Desglose suma, límites respetados y matches completos | allow; ALL_APPLICABLE_POLICIES_SATISFIED | EVAL con importes y cobertura |
| N05 | POL-05 / D2 | Total autoritativo excede max_total, manteniendo desglose válido | deny; CHECKOUT_CONSTRAINT_MISMATCH | firma/cierre=0; incluir envío; no ampliar presupuesto |
| F05 | POL-05 / D2 | Total bool/string, cargo no representable o falta un match | GOV_INPUT_INVALID o GOV_FACTS_UNAVAILABLE según parseo/proyección | firma/cierre=0; nunca omitir cargo/constraint |
| P06 | POL-06 / D2 | D1 y contexto normativo compatibles con binding actual | allow; ALL_APPLICABLE_POLICIES_SATISFIED | EVAL; versiones CAS de fase pueden diferir |
| N06 | POL-06 / D2 | D1 auténtico usa versión previa; motor actual correcto | deny; POLICY_CONTEXT_MISMATCH | firma/cierre=0; D1 debe reevaluarse |
| F06 | POL-06 / D2 | Motor responde allow pero artefacto desplegado no es esperado | GOV_ARTIFACT_MISMATCH | firma/cierre=0; eco de expected_policy no basta |
| P07 | POL-07 / D2-HNP | Grant activo y vigente, owner/Agent Key vinculados | allow; ALL_APPLICABLE_POLICIES_SATISFIED | EVAL con grant/epoch/prueba verificada |
| N07 | POL-07 / D2-HNP | Grant revoked; variante adicional now=expires_at | deny; DELEGATION_NOT_ACTIVE | reserva/cierre=0; límite temporal exclusivo |
| F07 | POL-07 / D2-HNP | No se resuelve estado de grant o perfil de prueba unresolved | GOV_FACTS_UNAVAILABLE o GOV_AUTHORIZATION_PROFILE_UNRESOLVED | reserva/cierre=0; no sustituir por HP ficticio |
| P08 | POL-08 / D2-HNP | Todos los términos e instrumento dentro del scope | allow; ALL_APPLICABLE_POLICIES_SATISFIED | EVAL con scope completo tipado |
| N08 | POL-08 / D2-HNP | Cambiar solo destino ligado; repetir con merchant/variante/total/instrumento fuera de scope | deny; DELEGATION_SCOPE_EXCEEDED | cierre=0; no consentimiento implícito del chat |
| F08 | POL-08 / D2-HNP | Constraint desconocido o proyección no soportada | GOV_INPUT_INVALID o GOV_AUTHORIZATION_PROFILE_UNRESOLVED | cierre=0; error de perfil, no constraint ignorado |
| P09 | POL-09 / D2 y D3-HNP | Capacidad libre en D2; reserva propia válida en D3; retry no suma dos veces | allow; ALL_APPLICABLE_POLICIES_SATISFIED | APPLY: una reserva y misma operación |
| N09 | POL-09 / D2 y D3-HNP | Consumo previo, reserva ajena o monto propio distinto | deny; DELEGATION_USE_UNAVAILABLE | cierre/commit=0; EVAL de contadores/refs |
| F09 | POL-09 / D2 y D3-HNP | Dos allows simultáneos; CAS de una operación pierde | decisión original no se reescribe; aplicación GOV_STATE_CONFLICT | solo un ganador; APPLY del perdedor no avanza |
| P10 | POL-10 / D3-HNP | D2/reserva/epoch/términos/versiones actuales coinciden | allow; ALL_APPLICABLE_POLICIES_SATISFIED | APPLY + un compromiso durable/outbox |
| N10 | POL-10 / D3-HNP | Epoch actual distinto al autorizado en D2/reserva, resto tipado | deny; DISPATCH_AUTHORITY_CHANGED; POL-09 también falla si reserva perdió vigencia | despacho=0; ambas razones ordenadas cuando aplican |
| F10 | POL-10 / D3-HNP | Revocación entre allow D3 y commit CAS, o SQLite falla | aplicación GOV_STATE_CONFLICT o GOV_EVIDENCE_UNAVAILABLE | sin outbox/efecto; no convertir allow en permiso durable |

Donde una fila enumera alternativas, cada alternativa será un caso parametrizado
con una frontera de fallo y una razón fijadas antes de ejecución; no aceptar cualquiera
de los códigos indiscriminadamente. En N10 no se espera una lista de razones de tamaño
uno si también incumple POL-09. El catálogo obliga a evaluar todas las aplicables.

## 3. Contratos y fallos comunes

| Caso | Inyección / condición | Criterio propuesto |
| --- | --- | --- |
| C01 | Campos extra, enums/versión desconocidos, JSON con claves duplicadas, required null | Rechazo estricto; motor no invocado; ninguna compra/firma |
| C02 | HP con hnp o HNP con hp/human_approved | Unión rechazada; no fallback entre modos |
| C03 | Razón desconocida, obligations arbitrarias, allow con regla fail, reglas faltantes/duplicadas | GOV_OUTPUT_INVALID; no aplicar salida |
| C04 | Timeout, HTTP 500, 200 sin result, JSON inválido, desconexión o excepción Python | GOV_ENGINE_UNAVAILABLE/GOV_OUTPUT_INVALID según frontera; enforce bloquea, shadow observa pérdida |
| C05 | Alterar un hecho, usar digest de otra entrada o estado esperado ajeno | Verificación digest/relación falla; no reutilizar allow |
| C06 | Mismo input y artefacto en motores Python y OPA | Igual outcome/reasons/rule_results; latencia/IDs internos no se comparan como política |
| C07 | Replay de bytes canónicos; permutar orden de claves de objetos | Digest estable tras canonicalización; ordenar solo colecciones declaradas conjunto |
| C08 | Reordenar lista semántica, cambiar importe/owner/grant/version | Digest distinto; sin autorización trasplantada |
| C09 | Doble evidence_id con distinto cuerpo o secuencia concurrente | Conflicto; no sobrescribir historial; unicidad comprobada por store |
| C10 | Input inválido contiene dirección/JWT/prompt adversarial | Ningún valor sensible en rechazo/ledger/log; solo campos permitidos y rutas seguras |
| C11 | Fallo evidencia luego de allow, antes de transición | Enforce: transición/effect=0; shadow: observación perdida sin bloquear conducta base |
| C12 | Payload/colecciones grandes | Fijar límites y probarlos bajo D32; pendiente, no reclamar protección DoS por esquema solamente |

C04 se comparte con las pruebas del ADR. Pruebas de parser no prueban motor real;
pruebas de motor no prueban persistencia, criptografía ni servicios externos.

## 4. Relación con S01–S05 sin alterar B0

| Escenario | Oráculo del tratamiento propuesto | Política / control y atribución |
| --- | --- | --- |
| S01 | Preservar compra elegible y espera HP; ningún efecto anticipado | POL-01–06, guardas HP/AP2 e idempotencia comunes; no exigir mejora donde B0 ya pasa |
| S02 | Explicación canónica, cero checkout, sin 5xx | Recuperación de ConversationService antes de D1; POL-01 cubre selección inválida si llega al servicio. No inventar evento PDP cuando no se evaluó |
| S03 | Pedir información faltante sin selección/checkout | require_user_input del orquestador, no outcome del motor ni consentimiento humano |
| S04 | No aceptar clic ligado a términos materiales obsoletos; nueva presentación | D17 + intento HP + POL-04/05; AP2/merchant siguen barrera final. El rechazo posterior del pago no subsana aceptación obsoleta |
| S05 | Datos de catálogo no alteran intención, autoridad, claves ni política | Filtros/assembler/superficie cerrada y POL-01/02/05; no inferir resistencia general a inyección por un caso |

S02 requiere cambiar recuperación respecto al `_recover_no_candidate` actual que usa
router: es mecanismo de tratamiento separado, no mejora que se atribuya a OPA. El modo
off/shadow debe conservar la conducta base, incluidos sus fallos conocidos; no introducir
la recuperación nueva en B0 por accidente. Lo mismo aplica a refresh que muta merchant.
La matriz y resultados originales permanecen intactos. Un nuevo manifiesto debe
identificar variantes/factores antes de evaluar; este documento no los ejecuta.

## 5. Casos HNP y frescura que cierran gates técnicos

| Caso | Secuencia controlada | Oráculo y gate |
| --- | --- | --- |
| HNP01 | Grant antes de valid_from, en expires_at y después | No cerrar; POL-07; reloj de autoridad, no reloj comercial sintético |
| HNP02 | Precio/envío nuevo dentro y fuera del scope | Dentro: nuevo D2/cierre solo si no hay presentación incierta; fuera: POL-08 deny; D17/H01 |
| HNP03 | Revocar antes de D2, entre D2 y D3 y entre D3 y CAS | No reserva/cierre/compromiso según punto; epoch rechecado bajo UoW; D23/H05 |
| HNP04 | Revocar después de compromiso durable | Operación en curso, no promesa de cancelación remota; conservar refs y reconciliar; H05 |
| HNP05 | Dos checkouts y claves distintas intentan consumir mismo grant | Una reserva/cierre vigente; idempotencia por checkout no basta; POL-09 + H04/D23 |
| HNP06 | Mismo operation_id y reserva reintentados | No duplicar consumo ni generar cadena nueva; POL-09/10 + D22/D24 |
| HNP07 | Firma falsa, cnf ajeno, hash JWT alterado, vínculo abierto/cerrado incorrecto | Verificadores independientes rechazan aunque el PDP dé allow; H01/H03 |
| HNP08 | Constraint desconocido o extensión propia no verificada | No cerrar/presentar; registrar brecha de perfil; H01/H02 |
| HNP09 | Crash tras reserva, tras firma, tras outbox o tras pago con orden desconocida | Reinicio conserva operación/reserva/cadena y consulta antes de nuevo efecto; D23/D24/H06 |
| HNP10 | Timeout tras primera presentación a PSP/merchant | Mantener reserva incierta; no recotizar/cerrar de nuevo sin reconciliación; H04/H05 |
| FR01 | Refresh inocuo con revisión/timestamp técnico de oferta cambiados | Preservar consentimiento y objeto firmado según contrato D17 elegido; sin bucle HP |
| FR02 | Cambio material de precio, envío, destino, variante o vigencia | HP: otro snapshot/intento/clic; HNP: reevaluar todos los límites, no ampliar scope |
| FR03 | GET devuelve snapshot anterior a cambio del catálogo | Nunca marcarlo current solo por GET exitoso; D17 |
| FR04 | Stock agotado/expiración entre D2 y complete | Merchant rechaza en su frontera; no éxito inventado ni segundo pago; D17/D24 |

Para HNP07 se necesitan dos niveles de pruebas: fixtures offline del bridge y
verificación independiente en merchant/proveedor. Aquí solo se especifican; ninguna
afirmación de conformidad HNP procede de parsear modelos o simular status=valid.

### 5.1 Mapeo de HNP01–HNP10 a los PRs de HNP

Agregado el 20-oct-2026 con la spec [`hnp-governed.md`](../design/hnp-governed.md) (HNP-00). No
cambia ningún caso ni oráculo: dice qué PR cubre cada caso y en qué capa. Los ids son los del plan
de PRs de esa spec; «I1» es la prueba en vivo (`tests/integration`, a mano con `RUN_LIVE_STACK=1`),
que cierra los gates H01/H04/H05/H06 en vivo.

| Caso | Store y reglas (sin servicios) | Servicio, executor y verificadores | En vivo |
| --- | --- | --- | --- |
| HNP01 | HNP-02 (vencimiento en el claim y en las UoW), HNP-03 (POL-07: antes de `valid_from`, `now == expires_at` y después deniegan) | HNP-06 (POL-07 por el boundary), HNP-09a (claim de un grant vencido: 0 checkout), HNP-M1 y HNP-P1 (`exp` del abierto con tolerancia de 60 s) | I1; HN04 en la evaluación (HNP-E1/E2) |
| HNP02 | HNP-03 (POL-08 con total, envío y demás términos) | HNP-06 (POL-05 y POL-08 por el boundary), HNP-09a (frescura `close`: cambio → escalamiento sin firma; dentro del alcance, D2 y cierre sobre los términos nuevos solo sin presentación incierta) | I1 (precio sobre el límite: escalamiento y 0 firmas); HN02 y HN03 en la evaluación |
| HNP03 | HNP-02 (revocar antes del compromiso no deja outbox; epoch re-chequeado en UoW-1 y UoW-2) | HNP-08 (revocación idempotente), HNP-09a (revocación antes de UoW-1 y entre UoW-1 y UoW-2), HNP-06 (N10/F10) | I1 (revocación con `FaultProxy` en `/ap2/checkout-jwt`: 0 órdenes y 0 tokens) |
| HNP04 | HNP-02 (revocar después del compromiso deja la operación «en curso») | HNP-08 (`RevokeReceipt` con `in_flight`), HNP-09b (el despacho sigue y se reconcilia; sin promesa de cancelar) | I1 |
| HNP05 | HNP-02 (de dos reservas sobre el mismo grant gana una; índice `one_use_per_grant`) | HNP-09a (conflicto CAS: el perdedor no avanza), HNP-03 (N09 con reserva ajena) | I1 (dos disparos: 1 orden); HN07 en la evaluación |
| HNP06 | HNP-02 (reintentar la misma operación devuelve la misma reserva, sin doble conteo) | HNP-09b (mismo `operation_id`, claves `:payment` y `:complete`; sin cadena nueva al reanudar) | I1 |
| HNP07 | HNP-04 (fixtures de oro: firma ajena, `cnf` ajeno, `sd_hash` de otro abierto, `checkout_hash` alterado) | HNP-M1 y HNP-P1 (verificación independiente con recibo de error) | I1 (cadenas alteradas enviadas directo al comercio y al PSP) |
| HNP08 | HNP-04 (restricción desconocida rechazada) | HNP-M1 y HNP-P1 (`unresolved_constraint`), HNP-09b (escala y no presenta otra vez) | I1 |
| HNP09 | HNP-02 (el estado sobrevive al reinicio) | HNP-09b (crash después de la reserva, del cierre, del outbox o del token: sin cierre nuevo; consulta antes de otro efecto) | I1 |
| HNP10 | HNP-02 (timeout → `uncertain` sin liberar) | HNP-09b (timeout después de la primera presentación: reserva retenida, sin re-cotizar ni cerrar de nuevo) | I1 |

Los P/N/F de POL-07..10 (§2) los cubren HNP-03 (reglas puras) y HNP-06 (boundary y evidencia); C01–C05,
C09 y C11 (§3), HNP-06; la cadena de evidencia de cada caso, HNP-V1.

## 6. Matriz de modos y comparabilidad

| Caso | off | shadow | enforce |
| --- | --- | --- | --- |
| M01: deny del PDP añadido | Sin evaluación/registro ficticio | Se registra, no bloquea base | Bloquea efecto protegido |
| M02: motor/evidencia añadida falla | Sin dependencia añadida | Continúa base, cuenta pérdida | Bloquea; no fallback automático |
| M03: ownership/AP2/persistencia operativa falla | Bloquea por guarda común | Bloquea por guarda común | Bloquea por guarda común |
| M04: observación de frescura | B0 sin cambios | Sin mutación; o condición distinta identificada | Mecanismo D17 del tratamiento fijado |
| M05: grant inválido sin PDP | No existe HNP en B0 HP; base HNP futura debe bloquear | Base HNP debe bloquear | Base HNP y B2 bloquean |
| M06: cambio de modo/bundle a mitad de run | Rechazar cambio implícito | Rechazar cambio implícito | Reevaluación explícita bajo contexto revisado, nunca degradación |

Primero medir paquete B2 y contribuciones separadas: PDP, frescura, evidencia,
recuperación y controles HNP comunes. Para atribuir beneficio al motor OPA, comparar
con Python equivalente y mantener los demás mecanismos. No comparar HP contra HNP
y llamarlo efecto de gobernanza. Declarar cobertura y denominadores por modo; datos
omitidos/UNKNOWN no se cuentan como éxito ni se completan con inferencias del runner.

## 7. Criterios de aceptación y orden de implementación

Estos son gates futuros, no resultados medidos:

1. Revisión humana de modelos, diez fichas, razones, privacidad y dueños humanos.
2. Primer incremento aislado: D1 estricto/puerto/canonicalización y referencia Python
   de prueba POL-01/02. P01/N01/F01/P02/N02/F02 y C01/C03/C05/C07/C08 en fixtures
   offline; ninguna conexión a runtime, DB, signer o merchant. H01 no es dependencia.
3. D11: equivalencia Python/OPA sobre corpus versionado con fallos; latencia y budgets
   medidos/revisados; cero allow aplicado ante fallos. No instalar OPA en EC-GOV-01.
4. D17/D18/D19/D23 HP antes de insertar D2 en compra: FR01–04, intentos/replay,
   evidencia+transición y crash points. No migrar datos existentes sin ticket.
5. H01/H03/H04/H05/H06 antes de HNP integrado: HNP01–10 y verificación independiente.
6. Enforce requiere cero efecto protegido en todos los rechazos/fallos de su corpus,
   replay completo de entradas válidas, cero secretos detectados y guardas comunes
   preservadas. Una prueba de ausencia solo vale en el perímetro realmente observado.
7. Los objetivos históricos de piloto (S04 0 aceptaciones obsoletas, S02/S03 5/5 útiles,
   traza ≥0,95) son candidatos heredados, no éxitos ni metas aprobadas por este texto.
   Denominador de traza/métricas y corpus reservado deben fijarse antes de medir.
8. Presupuesto de rendimiento, retención y límites se cierran con evidencia D11/D30/D32;
   sin números aprobados no hay gate de producción experimental satisfecho.

El Sprint 2/semana 11 fija prioridad, no permite saltar H01/D17/D23 para presentar
HNP como implementado. El gate D38 sigue abierto. Pruebas pendientes se conservan
como pendientes y los resultados B0 originales nunca se recalculan ni sobrescriben aquí.
