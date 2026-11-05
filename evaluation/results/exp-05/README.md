# EXP-05 — CT15 HNP y transporte autenticado

PR #168 directo a main, rama codex/exp-05-ct15-hnp desde
4ce219623ed0304a3e7378519a37f0b8844694c5. CI de esa base 41908376626, cuatro jobs success
comprobados antes de implementar. No deriva de ramas antiguas ni agrega una cadena de PRs.

## Resultados y procedencia

| Run | SHA ejecutado | Resultado original |
| --- | --- | --- |
| [41913321178](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41913321178) | 7394dab420d241296f0e15e227502e2bc6b0c760 | CT15: 2 failed, 2 errores teardown. Hook exigía quantity explícito ausente en el wire real. validated=false; 20 hashes verificados. |
| [41913671886](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41913671886) | f95610fbc68b1758f2bdfba9e6a4398763fe2fd9 | CT15: 2 passed, 0 failed/error/skipped, validated=true; 24 hashes verificados. |
| [41914629295](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41914629295) | 3cf817145ed8aa73c37e57121d4dd69a02076f97 | Transporte: pytest 1 passed; reportador rechazó AUTH-TRANSPORT fuera de plan P0. Sin informe/hash interno; no validación automática completa. ZIP íntegro conservado. |
| [41915169656](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41915169656) | f30e7c47f3c5e95584499eedc992c624b479a6a9 | Transporte: 1 passed, 0 failed/error/skipped, validated=true; 15 hashes verificados; cero atribuciones P0. |

En TODOS los runs Merchant abf30df3b6a611284dedcc26c00accac6a853329 y PSP
e6ab67b5963434a9a3caae2012b70232a3cca65d, checkouts limpios verificados por preflight.
Cada SHA Agent efectivo y hash lock/protocolo se conserva en summary.json y manifest original.
El HEAD documental posterior no sustituye los SHAs ejecutados.

## CT15 HNP: medición P0 nueva y alcance

HN03 histórico (evaluation/data/hnp-scenarios.json, hnp-pilot-v1.json y
hnp_pilot_episode._apply) reexporta el feed y reinicia Agent en ambas condiciones. Su evidencia
histórica permanece intacta: B2 puede ver el precio nuevo y escalar en D1 antes de checkout.
Aquí el feed completo previo (310 ofertas) permanece idéntico: el hook cambia precio DESPUÉS
de selección/D1 y antes del POST Merchant. No se refresca feed ni se reinicia Agent.
Por eso el caso por encima del máximo crea una sesión y la guarda autoritativa la cancela:
no se espera ni se inventa el resultado de HN03 para ese estímulo distinto.

Se observa X/otra oferta y se congela el reemplazo de precio antes de Agent. Ítem X pasa de
38,000,000 a otro precio Merchant observado, 42,000,000 centavos COP; envío 2,500,000;
total HTTP Merchant 44,500,000. Es intervención sintética controlada, no precio de mercado.
Snapshot completo garantiza que solo ítem/total/revisión cambian. Identidad, stock, envío,
contexto y fechas permanecen. La petición wire se verifica con build_checkout_create vigente;
la cantidad uno se verifica en la respuesta real, no se cambia el contrato para pasar.

above: máximo antiguo 40,500,000, una creación, cancelación real, failed/budget_guard y ninguna
reserva. within: máximo exacto 44,500,000, preparación válida; revocación de seguridad explícita
al pedir checkout-jwt. UoW-1 llega a reservar y queda released/no_outbox; tarea canceled/
revoked_by_owner. Ese canceled lo causa el control de seguridad, no el cambio de precio.
No se acredita compra autónoma dentro de presupuesto, solamente preparación y contención.
Sin mandatos cerrados, outbox, tokens, órdenes o pagos; todos los checkouts cancelados y ofertas
restauradas exactamente, Agent cerrado, proxy_closed/postgres_removed true.

## Adaptador autenticado: validado como transporte, semántica no ejecutada

Se extiende conversation.request_json/evaluate existente con AuthClient compatible vía
TokenSource. JWT solo en header; mismo transporte autenticado para cancelación. HTTP/network/
JSON inválido producen errores técnicos, nunca false comercial. No hay fallback sin token;
redirects autenticados rechazados y reflejos de secretos sanitizados por redaction existente.
Pruebas offline de propagación, cancelación, 401/503/302, red/JSON, token ausente y fuga.

Prueba live usa NodeServer/AgentServer existentes, AuthClient.signup/token real, auth sintético
real y JwtBuyerAuthenticator/JWKS de producción. Sin token/inválido 401; JWT válido GET historial
200; redirect real 307 rechazado. UX01/11/12 se envían como textos preparados a la ruta con
modelo desactivado: 502 intent_model_error. Cero purchases, approvals, model_requests, checkout,
órdenes/pagos. Merchant y PSP idénticos antes/después; auth sí escribe usuarios/sesiones/JWKS,
observados por conteos/huellas sin publicar DB ni claves/tokens. Procesos cerrados y stack limpio.

El reportador separa explícitamente AUTH-TRANSPORT de los IDs P0; rechaza controles desconocidos
y las pruebas de regresión garantizan que NO se sumen escenarios. attributed_scenarios={},
proyección P0 0 passed y 37 not_executed para ese perfil. No sustituye el progreso de otros runs.
UX01/UX11/UX12 semánticos permanecen not_executed. No se ejecutó evaluate-conversation CLI,
piloto live ni LLM pagado; no se guionaron aclaraciones del Agent para pasar.

## CT19 y decisiones pendientes

Ver docs/design/exp-05-ct19-feasibility.md. Contrato permite ausencia de identidad (404
invalid_item), distinto de stock cero (409 out_of_stock). Falta control reversible del arnés;
FK impide retirar oferta con checkout existente. No se implementó ni ejecutó CT19 aquí.
El diseño propone retirar antes de crear, después de selección, y restaurar snapshot exacto.

Para semántica: elegir modelo/versión y presupuesto autorizados, congelar protocolo/repeticiones/
rúbricas y fecha/zona para UX12; usar evaluate(..., auth=AuthClient(...)) con signup previo.
No se necesita otro runner equivalente. HP transporte validado; HNP conversacional pendiente.

Los cuatro ZIP originales y SHA256SUMS están versionados: failed/error y publicación incompleta
se conservan. No borrar ni sustituir. Los dos protocolos se mantienen idénticos desde sus
primeros commits; correcciones de arnés/reportador no cambian oráculos. No src/commerce_agent/,
proveedores, locks, matriz P0, oráculos/evidencias EXP-01–04 o pila P3 modificados. No trabajo
INF27/28/29/33 de Jair, no merges, no nuevos experimentos fuera de este lote.
