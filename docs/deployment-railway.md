# Despliegue sandbox en Railway

Este despliegue sigue siendo un experimento: un agente, un merchant ACP, un proveedor de pagos
sintético, una UI y PostgreSQL. No acepta tarjetas reales ni mueve dinero. Configure servicios y
secretos en un entorno aislado; no copie `.env` al repositorio.

## Servicios y almacenamiento

| Servicio | Código | Estado persistente | Healthcheck |
| --- | --- | --- | --- |
| UI | `ui/` de este repo | ninguno | `/` |
| Agente | raíz de este repo | volumen `/app/.local` | `/health` |
| Merchant | `tesis-commerce-merchant` | PostgreSQL privado | `/health/ready` |
| Proveedor | `tesis-payment-provider-sandbox` | volumen `/app/.local` | `/health` |

Configure la UI con root directory `/ui` y config file `/ui/railway.json`; los otros dos repos
usan su raíz. Railway [requiere especificar aparte la ruta del config file al usar root
directory](https://docs.railway.com/deployments/monorepo). Monte los volúmenes en los directorios
que contienen las bases SQLite: Railway no conserva archivos del contenedor entre despliegues
([documentación de volúmenes](https://docs.railway.com/volumes)).

## Variables que deben coincidir

- UI: `VITE_AGENT_API_URL` es el origen HTTPS público del agente. Se incorpora al JavaScript
  durante el build, no al arrancar Caddy.
- Agente: `AGENT_UI_ORIGIN` es el origen HTTPS público exacto de la UI;
  `MERCHANT_BASE_URL` es el origen HTTPS público del merchant;
  `PAYMENT_PROVIDER_URL` es el origen HTTPS público del proveedor;
  `AGENT_FEED_PATH=/app/deploy/feed/p0` y `AGENT_DATABASE_PATH=/app/.local/agent.db`.
- Merchant: `ACP_API_BASE_URL` coincide con `MERCHANT_BASE_URL` del agente;
  `PAYMENT_PROVIDER_URL` coincide con el del agente; `WEBHOOK_RECEIVER_URL` es **solo el origen**
  HTTPS público del agente (el dispatcher agrega la ruta del webhook);
  `MERCHANT_ALLOW_REMOTE_DB=true` habilita únicamente PostgreSQL en `*.railway.internal`.
- Proveedor: `PAYMENT_DATABASE_PATH=/app/.local/payments.db`.
- El Bearer configurado como `MERCHANT_AGENT_BEARER_TOKEN` en el merchant es
  `MERCHANT_BEARER_TOKEN` en el agente. `MERCHANT_WEBHOOK_SECRET` es idéntico en ambos.
- `PAYMENT_AGENT_BEARER_TOKEN` coincide entre agente y proveedor;
  `PAYMENT_MERCHANT_BEARER_TOKEN`, entre merchant y proveedor. `PAYMENT_TOKEN_SECRET` solo existe
  en el proveedor. `PAYMENT_MERCHANT_ID` debe ser igual en los tres servicios.
- `OPENAI_API_KEY`, `OPENAI_MODEL` y `AGENT_APPROVAL_SECRET` viven únicamente en el agente.

Use una instancia PostgreSQL dedicada y privada. `DATABASE_URL` del merchant debe apuntar a
`/tesis_lab` en el host privado `*.railway.internal`; configúrela mediante una referencia de
servicio, no con un valor escrito en Git. Si esa base no existe, configure además
`BOOTSTRAP_ADMIN_DATABASE_URL` con una URL de la base administrativa de la misma instancia,
con permiso para crear `tesis_lab`. No publique PostgreSQL por TCP público. Railway describe las
[referencias de variables](https://docs.railway.com/variables/reference) y la [red privada entre
servicios](https://docs.railway.com/networking/private-networking).

Las URLs HTTP internas `*.railway.internal` no pasan los validadores actuales del agente y del
merchant, que exigen HTTPS fuera de loopback. Por eso estas variables usan dominios HTTPS públicos y
Bearer separados; la base de datos sí permanece en la red privada. No abra la UI al público hasta
haber conectado y probado todos los servicios.

## Reloj del episodio y verificación

El feed P0 está congelado en `2026-09-10T14:00:00Z` y las ofertas vencen quince minutos después.
Configure `AGENT_SANDBOX_CLOCK_AT=2026-09-10T14:05:00Z` y
`PAYMENT_SANDBOX_CLOCK_AT=2026-09-10T14:05:00Z`. El merchant usa el reloj del episodio en
PostgreSQL. Estos relojes son solo para la demostración sintética.

1. Confirme los cuatro healthchecks y `GET /.well-known/acp.json` en el merchant. La URL
   `api_base_url` anunciada debe ser exactamente `MERCHANT_BASE_URL` del agente.
2. Compruebe que la UI fue construida con el origen correcto del agente y que CORS permite
   exclusivamente el origen de la UI.
3. Ejecute una compra completa con aprobación humana, luego pago rechazado, stock agotado y
   proveedor indisponible. Una compra no debe declarar éxito sin orden autoritativa.
4. Despache un webhook pendiente y compruebe la orden en el agente. El merchant registra
   `Request-Id` para seguir los errores sin mostrar secretos.

El feed copiado en la imagen del agente es una instantánea, no una sincronización continua: después
de agotar inventario, vuelva a exportar y desplegar el feed antes de otra ronda de pruebas. No use
este entorno como tienda real.
