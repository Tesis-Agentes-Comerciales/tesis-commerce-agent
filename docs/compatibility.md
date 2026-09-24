# Compatibilidad y modelo de seguridad P0

## Contrato congelado

| Decisión | Implementación |
| --- | --- |
| ACP | `2026-04-17` |
| Transporte | REST |
| Catálogo | snapshot completo ACP leído desde `/feeds/current` del merchant antes de cada turno; archivos locales solo en pruebas |
| Moneda | `COP` en el feed actual, `cop` en checkout; los fixtures de prueba conservan `USD`/`usd` |
| Cantidad | una unidad |
| Pago | `tesis_sandbox`, credencial opaca `vt_*` emitida por el proveedor propio |
| E2E | agente, merchant y proveedor en procesos separados |
| Webhook | `t=<unix>,v1=<HMAC-SHA256>` sobre `timestamp + "." + raw_body` |

Todos los requests y responses ACP usados por el agente se validan contra el bundle vendorado. Los
mensajes del merchant se consideran datos no confiables y no se convierten en instrucciones.
`/feeds/current` es transporte de publicación propio del merchant, no la Feed API incremental
estándar. El agente valida cada producto contra el esquema ACP antes de usarlo. Si el feed falla,
el turno se detiene; el checkout vuelve a consultar precio, stock y total al merchant.

## Fronteras de autoridad

| Dato o efecto | Autoridad |
| --- | --- |
| Intención y preferencia | salida tipada del modelo conversacional; nunca autoridad comercial |
| Conjunto elegible | ranking determinista sobre feed validado |
| Candidato | elección tipada del modelo, limitada al conjunto elegible y revalidada antes de ACP |
| Precio, stock, envío y total | último checkout del merchant |
| Identidad humana | configuración/backend del agente |
| Aprobación | HMAC durable sobre términos exactos |
| Clave idempotente | SQLite del agente |
| Credencial sandbox | proveedor externo al agente y merchant; el agente no la persiste |
| Orden | respuesta o consulta autoritativa del merchant |
| Estado posterior | webhook firmado y proyección monotónica |

## Controles implementados

- HTTPS obligatorio fuera de loopback, redirects desactivados y proxies de entorno ignorados.
- Responses ACP limitadas a 2 MiB; errores remotos se normalizan sin copiar mensajes hostiles.
- Esquemas estrictos para entradas del modelo y payloads de checkout/payment.
- Fingerprint HMAC versionado que incluye comercio, protocolo, checkout, líneas, total, comprador,
  destino, fulfillment, `updated_at` y expiración.
- CAS SQLite `approved|uncertain → completing`; solo el ganador despacha `complete`.
- Claves idempotentes estables tras restart para delegación y completion; el proveedor decide el resultado.
- Timeout/transport marcan `uncertain`; la reanudación consulta el checkout antes de redespachar.
- Webhook autenticado antes de parsear, máximo 1 MiB, tolerancia simétrica de 300 segundos,
  comparación constante y dedupe por SHA-256 del body firmado.
- Los eventos fuera de orden se guardan, pero la proyección no regresa un estado conocido.
- `POST /assistant` trata el estado del cliente como no confiable: limita el body a 1 MiB, usa
  checkpoints de LangGraph por `conversation_id` como historial del modelo (máximo 20 mensajes;
  solo incorpora el último mensaje nuevo del usuario), relee compra y etapa de
  SQLite por `run_id` y nunca envía `system`, `tools`, tool calls ni razonamiento del cliente al
  modelo. Imagen y descripción del feed solo se proyectan para la UI (HTTPS, mismo host, sin
  credenciales, texto plano truncado) y no entran a los prompts. Los errores se traducen a
  mensajes fijos en español sin texto de excepciones.

## Hipótesis y desviaciones conocidas

1. **Un solo writer del Bearer.** ACP complete no ofrece `If-Match` para comparar el fingerprint
   aprobado bajo el lock del merchant. La garantía P0 requiere que solo este agente posea y escriba
   con el Bearer. El agente hace GET inmediato, comparación exacta y CAS local, pero otro holder del
   mismo Bearer podría crear una ventana TOCTOU.
2. **Coordinación local.** SQLite y el CAS son adecuados para el prototipo de un host. Una versión
   multi-instancia requiere PostgreSQL o un coordinador distribuido.
3. **Merchant único configurado.** La URL validada funciona como allowlist P0; un Merchant Registry
   persistente con altas y revocación queda para el siguiente incremento.
4. **Modelo externo.** El router conversacional es inyectable y su salida se valida como
   `CommerceDecision`. El selector solo ve candidatos previamente filtrados y su ID se revalida
   antes de crear un checkout. Solo el backend ve el `PurchaseIntent`; datos de entrega y autoridad
   de compra permanecen fuera del prompt.
5. **Evidencia parcial.** Se conservan solicitud, candidato, checkout, aprobación, operación, orden y
   eventos. El manifiesto experimental completo, métricas de modelo y comparación B0/B2 quedan fuera
   de las etapas implementadas aquí.
6. **Pago sintético.** El proveedor es un servicio propio con estado independiente, no un PSP real.
   Solo acepta tarjetas de prueba, no almacena PAN ni CVC y no mueve dinero. Este P0 no modela 3DS,
   devolución ni disputa. El checkout del merchant confirma contra el proveedor antes de crear la
   orden; un fallo tras la confirmación se recupera con la misma clave idempotente al reintentar.
7. **Hilos e identidad.** `conversation_id` no autentica por sí mismo. En modo público, Better Auth
   emite JWT cortos validados por la API; los identificadores de hilo/turno se derivan junto con el
   `sub` autenticado y cada compra se consulta por propietario. El historial completo de hilos no
   tiene todavía una vista de recuperación; solo se repinta la compra pendiente.

## Criterio de fallo seguro

Sin contrato válido, checkout listo, términos vigentes, aprobación exacta, reserva CAS o evidencia
autoritativa de orden, el agente no declara una compra completada. Un resultado desconocido se
mantiene explícito como `uncertain` y conserva la misma identidad de operación.
