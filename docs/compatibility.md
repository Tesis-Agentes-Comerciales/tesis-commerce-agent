# Compatibilidad y modelo de seguridad P0

## Contrato congelado

| Decisión | Implementación |
| --- | --- |
| ACP | `2026-04-17` |
| Transporte | REST |
| Catálogo | reemplazo completo `metadata.json` + `products.jsonl` |
| Moneda | `USD` en feed, `usd` en checkout |
| Cantidad | una unidad |
| Pago | `tesis_sandbox`, credencial `spt_test_*` |
| Merchant E2E | `Maldinho18/tesis-commerce-merchant@361bfe7436dd0167cad3b4b3fedbf79c82a9c76b` |
| Webhook | `t=<unix>,v1=<HMAC-SHA256>` sobre `timestamp + "." + raw_body` |

Todos los requests y responses ACP usados por el agente se validan contra el bundle vendorado. Los
mensajes del merchant se consideran datos no confiables y no se convierten en instrucciones.

## Fronteras de autoridad

| Dato o efecto | Autoridad |
| --- | --- |
| Intención y preferencia | salida tipada del modelo conversacional; nunca autoridad comercial |
| Candidato | ranking determinista sobre feed validado |
| Precio, stock, envío y total | último checkout del merchant |
| Identidad humana | configuración/backend del agente |
| Aprobación | HMAC durable sobre términos exactos |
| Clave idempotente | SQLite del agente |
| Credencial sandbox | adaptador backend; no se persiste |
| Orden | respuesta o consulta autoritativa del merchant |
| Estado posterior | webhook firmado y proyección monotónica |

## Controles implementados

- HTTPS obligatorio fuera de loopback, redirects desactivados y proxies de entorno ignorados.
- Responses ACP limitadas a 2 MiB; errores remotos se normalizan sin copiar mensajes hostiles.
- Esquemas estrictos para entradas del modelo y payloads de checkout/payment.
- Fingerprint HMAC versionado que incluye comercio, protocolo, checkout, líneas, total, comprador,
  destino, fulfillment, `updated_at` y expiración.
- CAS SQLite `approved|uncertain → completing`; solo el ganador despacha `complete`.
- Clave idempotente estable tras restart y pago sandbox derivado de la misma operación.
- Timeout/transport marcan `uncertain`; la reanudación consulta el checkout antes de redespachar.
- Webhook autenticado antes de parsear, máximo 1 MiB, tolerancia simétrica de 300 segundos,
  comparación constante y dedupe por SHA-256 del body firmado.
- Los eventos fuera de orden se guardan, pero la proyección no regresa un estado conocido.

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
   `CommerceDecision`. Solo el backend ve el `PurchaseIntent`; datos de entrega y autoridad de
   compra permanecen fuera del prompt.
5. **Evidencia parcial.** Se conservan solicitud, candidato, checkout, aprobación, operación, orden y
   eventos. El manifiesto experimental completo, métricas de modelo y comparación B0/B2 quedan fuera
   de las etapas implementadas aquí.
6. **Pago sintético.** No existe PSP real, PAN, CVC, 3DS, devolución o disputa.

## Criterio de fallo seguro

Sin contrato válido, checkout listo, términos vigentes, aprobación exacta, reserva CAS o evidencia
autoritativa de orden, el agente no declara una compra completada. Un resultado desconocido se
mantiene explícito como `uncertain` y conserva la misma identidad de operación.
