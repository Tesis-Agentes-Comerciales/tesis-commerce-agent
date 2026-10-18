# ADR provisional: motor de políticas B2 v1

Estado: **PROPUESTO; D11 ABIERTO**, EC-GOV-01, 2026-10-14.
No se eligió ni instaló un motor en esta tarea. No hay benchmark nuevo, cifras de
latencia medidas ni presupuesto operativo aprobado. Responsables humanos de
propuesta, comparación, revisión y aceptación: **por confirmar**.

## 1. Contexto y fuentes

La instrucción de definir políticas antes de programar y el plazo del prototipo v2
no convierten una tecnología en requisito aprobado. El
[roadmap HP/HNP](system-design-v1/roadmap.md) recomienda OPA local tras un puerto
síncrono y exige prueba D11/D32. El [registro](system-design-v1/decisions.json)
mantiene D11 en `needs_spike` y propietarios sin asignar.
La [revisión del 7 de octubre](design-review.md) recomienda dos decisiones HP y
advierte sobre duplicar validaciones. El [cierre del 11](design-closure-2026-10-11.md)
amplía alcance a HNP; no atribuye aprobación de OPA a los asesores.

Se usan como entradas de diseño el [contrato v1](policy-v1.md), su
[aceptación](policy-acceptance-v1.md), las [amenazas](threat-model.md), la
[evaluación anterior](evaluation-plan.md) y el
[manifiesto anterior](../../evaluation/b2-design.json). Las discrepancias P1–P5,
outcomes, timeout de 100 ms y motor declarado se registran en policy-v1; no se
reescriben documentos ni resultados históricos para presentar consenso inexistente.

## 2. Pregunta y recomendación provisional

¿OPA aporta separación de política, revisabilidad y valor experimental suficiente
para justificar otro proceso y su frontera de fallo, frente a Python tipado con
el mismo contrato y artefactos versionados?

**Conservar OPA/Rego local como candidato recomendado por el diseño final, detrás
de PolicyEvaluator; no declararlo seleccionado todavía.** Implementar primero,
si se autoriza otro ticket, los contratos D1 y una referencia Python de prueba
aislada, útil para comparar motores. No activarla en runtime ni tratarla como
fallback automático ante OPA caído. H01 no es dependencia de ese trabajo puro.

La elección del motor no cambia las diez políticas, sus fuentes, la semántica de
modos, el ledger transaccional, AP2 o la necesidad de CAS. Un evaluador Python
puede tener políticas versionadas y evidencia; la separación de gobernanza no
exige por definición un proceso HTTP.

## 3. Alternativas con criterios explícitos

| Criterio | OPA/Rego local | Python tipado detrás del mismo puerto |
| --- | --- | --- |
| Separación/revisión | Artefacto declarativo independiente; desplegar otro artefacto/motor exige control de versiones | Reglas como funciones puras versionadas; requiere disciplina para no mezclarlas con efectos |
| Testabilidad | Corpus común más pruebas del adaptador HTTP y contrato de resultado; Rego se prueba aparte | Fixtures directas y pruebas de esquemas/funciones; también debe probar identidad de artefacto y wrapper |
| Fail-closed | Tratar indisponibilidad, timeout, indefinido, salida inválida y artefacto erróneo | Tratar excepción, datos ausentes, salida inválida, versión errónea y evaluación excedida |
| Trazabilidad | Revisión/digest del artefacto y correlación del motor son complementarios al ledger | Revisión/digest del código y datos deben conservarse explícitamente junto con la decisión |
| Latencia | Añade serialización/transporte/proceso y readiness; magnitud pendiente de medición | Evita transporte; magnitud y cola bajo carga también pendientes |
| Consistencia | Lee hechos enviados; no convierte un snapshot en lock de grant/stock | La cercanía al store tampoco sustituye CAS o una UoW |
| Operación | Otro proceso/configuración/fallo que sostener en una instancia experimental | Menor topología; timeout/cancelación de una función bloqueante deben resolverse, no asumirse |
| Experimento | Permite estudiar policy-as-code externo, si esa es una hipótesis acordada | Control equivalente para aislar el valor del motor del resto del paquete B2 |
| Coste de cambio | Rego y gestión de artefactos introducen conocimientos/configuración específicos | Acoplamiento al lenguaje del agente, mitigado por puerto y corpus estable |

Cedar no se descarta por incapacidad: es una alternativa de autorización
principal/acción/recurso mencionada por el roadmap. No se incorpora un tercer
spike sin requisito o brecha concreta que lo justifique. Tampoco se añaden
MCP, A2A, UCP ni otros frameworks para elegir el PDP. ACP/UCP corresponde a H01/D04,
no a D11.

## 4. Protocolo propuesto para cerrar D11

Este protocolo describe una prueba futura, no autoriza ejecutarla en EC-GOV-01.

### Preparación y preregistro

1. Revisar contrato y corpus; fijar versiones exactas de motor, adaptador,
   artefacto, Python, host y parámetros. No elegir aquí una versión OPA sin prueba.
2. Acordar la variable experimental: paquete de gobernanza o motor externo.
   Si se atribuye efecto a OPA, Python equivalente es control obligatorio.
3. Antes de comparar resultados, fijar presupuestos máximos de p95/p99, timeout,
   sobrecosto end-to-end, concurrencia, tamaño de entrada y pérdida de observación.
   Sus valores están **PENDIENTES**; usar 100 ms del borrador como aprobado sería falso.
4. Fijar número de repeticiones, calentamiento, orden alternado, escenarios de
   carga y forma de tratar cold starts. No escoger el corpus después de ver qué gana.

### Equivalencia y fallos

Ejecutar entradas idénticas tipadas de P01–P10/N01–N10/F01–F10 y C01–C12 de la
[matriz](policy-acceptance-v1.md). Las ramas HNP pueden usar hechos simulados para
comparar reglas, rotulados como tales; no acreditan firmas ni bridge H01.
Normalizar solo campos operativos fuera del resultado lógico: latencia, ID interno
y hora de ejecución. Outcome, razones ordenadas y rule_results deben coincidir.

Inyectar OPA inaccesible/lento, reinicio, HTTP 500, 200 sin `result`, JSON/esquema
inválido, perfil desconocido y mismatch de artefacto; en Python, excepción,
resultado ausente/inválido, código/artefacto distinto y evaluación bloqueante.
No aceptar una única estrategia de timeout sin probar que su trabajo no queda
ejecutándose de forma que permita efectos o agote recursos. Ningún evaluador posee
efectos, pero las colas y workers del adaptador también deben estar acotados.

Comprobar readiness con identidad real del artefacto cargado, no solo disponibilidad
del puerto ni eco de versión. Para la ruta OPA propuesta se validará el contrato
de `result` y ausencia de resultados indefinidos descrito por el roadmap; HTTP 200
no habilita compra. Logs del motor no sustituyen DecisionEvidence antes de transición.

### Mediciones y decisión humana

Registrar p50/p95/p99 de evaluar y del camino completo, cold/warm, tamaño de JSON,
serialización, colas, timeouts, uso de recursos y sobrecosto de operar el proceso.
Medir sin llamadas LLM para aislar el motor y, después, por integración autorizada
para coste end-to-end. No hacer pasar un percentil del motor por latencia de compra.

Gate propuesto de D11:

- Cero discrepancias de decisión en el corpus fijado, o discrepancias explicadas y
  corregidas antes de comparar desempeño; no excluir casos fallidos a posteriori.
- Cero aplicación de allow ante fallo/indefinido/artefacto incorrecto en enforce.
- Los presupuestos acordados antes de medir se cumplen, incluida recuperación de
  proceso y evaluación bajo carga; sin budgets no hay PASS cuantitativo.
- Evidencia replayable, ausencia de secretos y modo shadow sin efectos nuevos.
- Ventaja concreta de OPA identificada: revisión/despliegue independiente o hipótesis
  experimental explícita; no basta afirmar que está de moda o que es "externo".
- Revisión humana firma el ADR con versión, corpus, resultados y motivos; hasta
  entonces ambos motores siguen candidatos y D11 permanece abierto.

Si OPA cumple equivalencia/budgets y aporta ese valor, confirmarlo. Si no lo aporta
o excede los presupuestos sin ventaja justificada, recomendar Python tipado como
motor de esa versión experimental; registrar la desviación del roadmap y revisar
el manifiesto futuro. No degradar automáticamente de OPA a Python durante una compra.

## 5. Relación con H01, D17 y D23

- H01 define qué representa autorización AP2 HNP; no condiciona la pertenencia D1,
  las razones ni el puerto. Hechos HNP simulados sirven para probar contrato,
  nunca para congelar el protocolo ni habilitar ejecución autónoma.
- D17 fija materialidad y vigencia; OPA no recotiza y Python tampoco debe inventar
  current a partir de GET. Mejorar S04 mediante frescura no prueba ventaja de OPA.
- D23 controla transición/evidencia/reserva/commit; un allow de cualquiera de los
  motores necesita la misma UoW/CAS. Más logs no previenen por sí solos doble uso.
- Cambios en recuperación S02, ledger o frescura son factores separados del motor;
  medirlos como paquete o con controles/ablaciones explícitos, preservando B0.

## 6. Consecuencias y límite de este ADR

La recomendación permite revisar políticas ahora y avanzar después con un pequeño
contrato puro sin esperar H01. No autoriza integración, instalar OPA, fijar SLO
inventados ni presentar HNP como implementado. Operación distribuida, retención,
límites de entrada y responsables siguen sujetos a D23/D30/D32 y revisión humana.
El plazo académico prioriza resolver incertidumbres, no sustituye su evidencia.
