# Despliegue sandbox en Railway

Este despliegue sigue siendo un experimento: un agente, un merchant ACP, un proveedor de pagos
sintético, Better Auth, una UI y PostgreSQL. No acepta tarjetas reales ni mueve dinero. Configure servicios y
secretos en un entorno aislado; no copie `.env` al repositorio.

> El acceso por correo y contraseña permite una demo pública **solo con datos y dinero sintéticos**.
> El registro está abierto y no verifica correo. No lo utilice para compradores reales ni pagos reales.
> Better Auth y el agente usan SQLite local persistente: despliegue una sola réplica por servicio.

## Servicios y almacenamiento

| Servicio | Código | Estado persistente | Healthcheck |
| --- | --- | --- | --- |
| UI | `ui/` de este repo | ninguno | `/` |
| Autenticación | `auth/` de este repo | volumen `/data` | `/api/auth/jwks` |
| Agente | raíz de este repo | volumen `/data` | `/health` |
| Merchant | `tesis-commerce-merchant` | PostgreSQL privado | `/health/ready` |
| Proveedor | `tesis-payment-provider-sandbox` | volumen `/data` | `/health` |

Configure la UI con root directory `/ui` y config file `/ui/railway.json`; autenticación usa
`/auth` y `/auth/railway.json`. Los otros repos usan su raíz. Railway
[requiere especificar aparte la ruta del config file al usar root
directory](https://docs.railway.com/deployments/monorepo). Monte los volúmenes en los directorios
que contienen las bases SQLite: Railway no conserva archivos del contenedor entre despliegues
([documentación de volúmenes](https://docs.railway.com/volumes)).

## Variables que deben coincidir

- UI: `VITE_AGENT_API_URL` es el origen HTTPS público del agente. Se incorpora al JavaScript
  durante el build, no al arrancar Caddy. `AUTH_UPSTREAM` debe apuntar al host y puerto internos
  de Better Auth (por ejemplo, `auth.railway.internal:8080`); Caddy sirve `/api/auth/*` bajo el
  mismo origen público de la UI. No exponga el servicio de autenticación directamente.
- Autenticación: `BETTER_AUTH_URL` es exactamente el origen HTTPS público de la UI;
  `BETTER_AUTH_SECRET` es aleatorio de al menos 32 caracteres y
  `AUTH_DATABASE_PATH=/data/auth.db`. Mantenga el volumen y este secreto entre despliegues.
- Agente: `AGENT_UI_ORIGIN` es el origen HTTPS público exacto de la UI;
  `AGENT_AUTH_MODE=jwt` y `AGENT_AUTH_ISSUER` es el mismo origen que `BETTER_AUTH_URL`;
  `AGENT_AP2_REQUIRED=true` y `AGENT_AP2_PRIVATE_KEY_PEM` es una clave EC P-256 privada;
  `MERCHANT_BASE_URL` es el origen HTTPS público del merchant;
  `PAYMENT_PROVIDER_URL` es el origen HTTPS público del proveedor;
  `AGENT_FEED_SOURCE=merchant` y `AGENT_DATABASE_PATH=/data/agent.db`. El agente consulta
  `/feeds/current` en el mismo origen configurado como `MERCHANT_BASE_URL`; no necesita un feed
  empaquetado ni una sincronización manual tras cambios de precio o inventario.
- Merchant: `ACP_API_BASE_URL` coincide con `MERCHANT_BASE_URL` del agente;
  `MERCHANT_AP2_PRIVATE_KEY_PEM` guarda una clave privada EC P-256 para firmar checkout JWT;
  `MERCHANT_AP2_REQUIRED=true` y `MERCHANT_AP2_AGENT_JWK_JSON` fija la clave pública del agente;
  `PAYMENT_PROVIDER_URL` coincide con el del agente; `WEBHOOK_RECEIVER_URL` es **solo el origen**
  HTTPS público del agente (el dispatcher agrega la ruta del webhook);
  `MERCHANT_ALLOW_REMOTE_DB=true` habilita únicamente PostgreSQL en `*.railway.internal`.
- Proveedor: `PAYMENT_DATABASE_PATH=/data/payments.db`;
  `PAYMENT_AP2_REQUIRED=true`, `PAYMENT_AP2_AGENT_JWK_JSON` y
  `PAYMENT_AP2_MERCHANT_JWK_JSON` fijan las claves públicas respectivas;
  `PAYMENT_AP2_MERCHANT_ISSUER` es exactamente `ACP_API_BASE_URL` y
  `PAYMENT_AP2_PRIVATE_KEY_PEM` es una tercera clave EC P-256 privada.
- El Bearer configurado como `MERCHANT_AGENT_BEARER_TOKEN` en el merchant es
  `MERCHANT_BEARER_TOKEN` en el agente. `MERCHANT_WEBHOOK_SECRET` es idéntico en ambos.
- `PAYMENT_AGENT_BEARER_TOKEN` coincide entre agente y proveedor;
  `PAYMENT_MERCHANT_BEARER_TOKEN`, entre merchant y proveedor. `PAYMENT_TOKEN_SECRET` solo existe
  en el proveedor. `PAYMENT_MERCHANT_ID` debe ser igual en los tres servicios.
- `ANTHROPIC_API_KEY`, `AGENT_MODEL` y `AGENT_APPROVAL_SECRET` viven únicamente en el agente.

Use una instancia PostgreSQL dedicada y privada. `DATABASE_URL` del merchant debe apuntar a
`/tesis_lab` en el host privado `*.railway.internal`; configúrela mediante una referencia de
servicio, no con un valor escrito en Git. Si esa base no existe, configure además
`BOOTSTRAP_ADMIN_DATABASE_URL` con una URL de la base administrativa de la misma instancia,
con permiso para crear `tesis_lab`. No publique PostgreSQL por TCP público. Railway describe las
[referencias de variables](https://docs.railway.com/variables/reference) y la [red privada entre
servicios](https://docs.railway.com/networking/private-networking).

Las URLs HTTP internas `*.railway.internal` no pasan los validadores actuales del agente y del
merchant, que exigen HTTPS fuera de loopback. Por eso estas variables usan dominios HTTPS públicos y
Bearer separados; la base de datos y Better Auth permanecen en la red privada. La API del agente
rechaza el modo de autenticación local en Railway o con una UI HTTPS. Cada ruta de compra comprueba
la propiedad por `sub` del JWT, además de su firma, emisor, audiencia y vencimiento. Los JWT duran
cinco minutos: cerrar sesión no revoca instantáneamente un JWT ya emitido. Use HTTPS y no registre
tokens en logs.

## Verificación

El merchant administra precios e inventario en PostgreSQL y publica un feed con vencimientos
calculados con el reloj real. No configure relojes fijos en el despliegue normal. Los JWT y
mandatos AP2 también usan el reloj real y tienen vencimientos cortos.

1. Confirme los cinco healthchecks, `GET /.well-known/acp.json` y
   `GET /.well-known/ap2/jwks.json` en agente, merchant y proveedor. Las claves verificadoras
   fijadas deben coincidir con esas respuestas. La URL
   `api_base_url` anunciada debe ser exactamente `MERCHANT_BASE_URL` del agente.
2. Cree dos cuentas de demo. Compruebe que `/api/auth/token` da un JWT para cada una, que la UI
   fue construida con el origen correcto del agente y que la cuenta B no puede recuperar,
   aprobar, cancelar ni completar la compra de A. Tras recargar la UI de A, la tarjeta de aprobación
   pendiente debe reaparecer.
3. Ejecute una compra completa con aprobación humana, luego pago rechazado, stock agotado y
   proveedor indisponible. Una compra no debe declarar éxito sin orden autoritativa.
4. Despache un webhook pendiente y compruebe la orden en el agente. El merchant registra
   `Request-Id` para seguir los errores sin mostrar secretos.

El agente consulta el feed publicado por el merchant antes de cada turno; los cambios de precio o
inventario ya publicados no requieren redesplegar el agente. No use este entorno como tienda real.

El [perfil AP2 Human Present](ap2-profile.md) implementa mandatos y recibos verificables para
pagos sintéticos. No anuncie este despliegue como conformidad AP2 integral ni como proveedor de
pagos reales. Active los tres flags AP2 juntos; una configuración parcial debe fallar cerrado.
