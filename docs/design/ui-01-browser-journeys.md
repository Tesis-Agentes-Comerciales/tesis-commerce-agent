# UI-01: recorridos de navegador históricos

Alcance autorizado: diagnosticar los cinco fallos de 41762122621 y 41653872780,
preservar esos resultados y corregir el recorrido o el producto según evidencia.
No ejecuta UX03/UX05 ni modelos; solo las compras sintéticas ya existentes.

La reproducción inicial mantiene las aserciones originales y añade trazas sanitizadas,
capturas, vídeo y estado de los controles. La corrección se contrasta con
`hnp-chat-delegate.md` (modo Delegar en chat, lista en Mis delegaciones) y las historias
de la actividad de compra. Se conservan consentimiento, presupuesto, vigencia,
revocación, D1/D2/D3, recibos y efectos; un defecto descubierto se registra como fallo.

La rama del PR parte de main. Una rama temporal de validación combina esta corrección
con P2 5900b0277f6c7d46c91fddb5923c1d145ecb69d3, sin modificar P2, para ejecutar
los 95 casos y su puerta de evidencia. El módulo afectado se reproduce primero con
`-vv --durations=20`. Merchant y PSP conservan los SHAs históricos aprobados.

Las trazas publicadas omiten contraseñas y credenciales de sesión; conservan estados
comerciales y eventos UI. Los archivos originales de resultados no se modifican.

## Diagnóstico reproducido antes de corregir

[Run 41822098357](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41822098357),
Agent `e6e888ec3980d0b493fe8c3a44a9a2853e16481d`: 6 recogidas, 1 passed, 5 failed,
0 skipped. Se ejecutó el módulo completo con `-vv --durations=20`. La puerta full
conservó `validated=false` y 89 `not_executed`: este diagnóstico no valida full ni P2.
Artefacto `nightly-ui01-diagnostic-evidence`, ID 13317602141, SHA-256 ZIP
`e0a159098549e4f3bc76116260d309f36d1884f5fe0da8d718bdc3fb1590f1a4`.

| Prueba | Causa demostrada | Corrección del arnés |
| --- | --- | --- |
| `test_browser_b2_governed_purchase_shows_d1_and_d2_allowing_it` | `controls.jsonl`: actividad abierta antes del clic, cerrada después. El desmontaje animado explica que las comprobaciones puedan pasar brevemente antes del timeout histórico. | Comprobar `aria-expanded`; abrir solo si estaba cerrada y exigir que quede abierta. |
| `test_browser_hnp_delegation_buys_once_and_shows_the_order` | El selector cambia el modo; no abre el diálogo antiguo. Captura, traza y snapshot coinciden con el diseño aprobado. | Buscar en modo delegado, consentir en el hilo y abrir la lista con el botón independiente. |
| `test_browser_hnp_delegation_on_a_phone_fits_the_screen` | Mismo selector obsoleto; el control de desborde solo conocía el diálogo antiguo. | Mismo recorrido y comprobación de desborde también en el formulario y estado del hilo. |
| `test_browser_hnp_price_over_the_limit_asks_for_a_decision_and_prefills_the_chat` | Falla antes del cambio de precio por esperar el diálogo desde el selector. | Consentimiento actual; conservar repricing, POL-11, prefill exacto sin enviar y ausencia de efectos. |
| `test_browser_hnp_revoke_from_the_card_leaves_no_order` | Falla antes de revocar por esperar el diálogo desde el selector. | Consentir en chat y revocar en la tarjeta de la lista; conservar recibo, 409 y efectos cero. |

Son expectativas obsoletas del arnés respecto de la interfaz aprobada; no se ha
demostrado un defecto de producto como causa de esos cinco fallos. No se amplían timeouts.
Referencias: `hnp-chat-delegate.md` (aprobación del 22 de octubre, alcance 1–6),
`ui/src/App.stories.tsx` (`HnpDelegateInChat`, `HnpMyDelegations`),
`ui/src/components/assistant-ui/elements/thread.aui.stories.tsx` (`PurchaseCompleted`),
`ui/src/components/delegation/delegation-status-card.tsx` y `src/commerce_agent/trace/assembler.py`.

Las dos marcas históricas `xfail` por pago se sustituyen por requisitos normales:
el código actual ya obtiene `payment_authorized` de `receipt_recorded/payment/success`,
con pruebas offline existentes de su causalidad respecto de la orden. Se exige en pantalla
y en la traza live, con ambos recibos, D1/D2/D3 allow y exactamente una orden/checkout/token.
La adaptación del mensaje sin presupuesto elimina el total ficticio heredado del fixture
`PHONE_INTENT`; el comprador sigue escribiendo el mismo margen sobre el precio real del feed.
El router guionado falla si el turno delegado intenta elegir antes del consentimiento.

## Verificación y límites

Offline: 25 pruebas de sanitización y trazas HNP pasaron; Ruff y Pyright para Linux pasaron.
Pyright por defecto en Windows detecta los símbolos POSIX históricos; no se cambian para esta
unidad. El arnés live requiere Linux/macOS; la validación con servicios se hace en Ubuntu 24.04
en GitHub Actions. Los resultados live posteriores y el SHA exacto se registran en el PR #161.

El módulo conserva sus seis node IDs; el perfil full de la rama temporal conserva los 95 IDs,
sin cambiar `nightly-required-cases.json`, el scorer ni la puerta. Un módulo passing no basta:
la ejecución full debe tener 95 passed, cero fallos/errores/skips/xfail/xpass y evidencia completa.
La ejecución no cuenta como ninguna celda UX03/UX05 ni como P3-V01.

La revisión de aislamiento comprueba `PurchaseLane`, `BuyerUi`, `clean_environment` y los
SHAs fijados: listeners loopback, PostgreSQL Docker desechable, PSP con SQLite propia,
tarjetas sintéticas existentes y secretos generados por ejecución; sin configuración ni
credenciales LLM. No se modifica producto, Merchant, PSP, datos u oráculos.

Las evidencias nuevas se descargan en el directorio ignorado `.local/ui-01/<run>/` y se
conservan como artefactos de Actions por 14 días; no sustituyen ni editan los resultados
históricos. Antes de vencer esa retención debe archivarse el ZIP con su digest publicado.
