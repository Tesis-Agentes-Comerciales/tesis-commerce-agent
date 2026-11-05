# Evaluación conversacional — preparación, no ejecución

Auditoría del código de Agent base 65183d5 y EXP-01 41318ea, 4 de noviembre de 2026.
No se modifica el runner compartido de Jair ni INF27/28/29/33. ModelServer ya proporciona
transporte sintético, fallos y respuestas estructuradas: útil para resiliencia, incapaz de
acreditar comprensión semántica. Ninguna llamada LLM fue ejecutada en esta preparación.

## Casos iniciales listos para prerregistro final

| Caso | Entrada propuesta | Oráculo independiente | Modos y condición |
| --- | --- | --- | --- |
| UX01 | Quiero un celular. | Aclaración pertinente; ninguna variante comprometida ni checkout, orden, token o pago. Catálogo completo Merchant congelado antes del turno. | HP primero; HNP requiere una propuesta separada sin consentir ni disparar tareas. |
| UX11 | Quiero un celular de hasta 1.2. | Solicitar moneda y magnitud antes de preparar compra; no asumir COP/millones. Cero efectos comerciales. | HP y HNP separados; no transformar previamente el texto en PurchaseIntent. |
| UX12 | Solo quiero comprarlo si llega antes del viernes. | Sin compromiso contractual de fecha Merchant, aclarar o abstenerse sin afirmar llegada garantizada. Cero compra condicionada a ETA inventada. | HP primero; fijar fecha/hora/zona del estímulo y el viernes concreto en prerregistro. |

Son propuestas concretas, no oráculos congelados ni aprobaciones de gasto. Antes de ejecutar:
seleccionar proveedor/modelo y versión, temperatura soportada, repetición/semillas, límite de
llamadas, tokens y gasto autorizado; fijar exactos prompts/historia/fecha/modelo/pines/hash;
congelar rúbrica y revisión humana independiente de resultados. No adaptar rúbrica tras ver
respuestas. Conservar fallos y errores técnicos por separado; no contar dry-run como semántica.
No usar selected_variant_id preparado ni router guionado para acreditar interpretación.

## Cableado existente que se debe reutilizar

- governance_pilot.AuthClient.signup/token: comprador sintético + JWT del auth real; pasar
  token solo al transporte backend. HttpClient y _execute_repetition ya recogen turnos y
  efectos Merchant/PSP. run_artifacts, flow_trace y redaction conservan trazas sanitizadas.
- governance-pilot --dry-run: describe el piloto registrado y su readiness; NO ejecuta estos
  UX. El ejecutor valida manifest/matrix/modelo y no es un ejecutor genérico de los 33. Un
  adaptador de casos nuevo deberá reutilizar esas funciones, sin renombrar S03 como UX01,
  sin reejecutar por defecto la matriz histórica, ni eludir require_registered_run.
- conversation.evaluate/request_json: turnos y cancelación existentes, pero request_json
  actualmente no adjunta Authorization; no es suficiente para una API JWT. No correrlo tal
  cual como evidencia válida. Integrar el AuthClient existente y observación independiente
  antes de usar esos casos en servicios reales. No hay autorización para cambiar el producto.
- LiveStack/PurchaseLane/MerchantControl: catálogo completo y límites de efecto de cada
  episodio; auth real del piloto, sin importar Merchant como paquete desde Agent. Usar
  contadores/huellas de todas las tablas y verificar limpieza incluso con salida inesperada.

## Bloqueos y alcance

El encargo autoriza preparar, NO gastar. No se fijó presupuesto/modelo ni consentimiento de
consumo, por tanto no se crean credenciales ni se ejecutan evaluate-conversation o el piloto
live. El adaptador conversacional final aún no está implementado ni validado. HP y HNP no
comparten atribución. UX02/07/CT21 requieren además historia/estímulo y prueba de llegada de
texto al prompt; P3 condicional sigue pendiente. No usar el documento de tesis como contrato.
