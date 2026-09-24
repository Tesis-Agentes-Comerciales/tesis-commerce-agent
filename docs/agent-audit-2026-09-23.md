# Auditoría del agente — 2026-09-23

Revisión independiente del grafo, herramientas, API, persistencia, UI y límites de compra. La
revisión no sustituye una prueba de penetración ni autoriza abrir la API al público.

## Corregido en esta pasada

| Área | Hallazgo | Corrección / verificación |
| --- | --- | --- |
| LangGraph Studio | Los nodos condicionales aparecían sueltos porque el grafo no declaraba sus destinos posibles. | Rutas explícitas; la API local de Studio devuelve 12 nodos y 16 aristas, incluidas las 10 condicionales. |
| Herramienta LLM | La rama de respuestas con razonamiento en streaming omitía `tools` y `tool_choice`; el modelo no podía consultar el catálogo. | Las dos ramas usan las mismas opciones; test de la llamada y de la respuesta de la herramienta. |
| Catálogo | El resumen completo podía crecer sin límite y el lookup literal no recuperaba bien preguntas naturales. | Resumen acotado, coincidencia parcial por tokens y señal explícita cuando los productos son solo ejemplos de fallback. |
| Checkout | El total y las líneas se comprobaban al crear la sesión, pero podían cambiar al actualizar entrega. | Validación final de identidad, SKU, líneas y presupuesto autoritativo antes de mostrar la aprobación; tests de aumento de total, cambio de sesión y línea extra. |

La única herramienta invocable por el modelo es `lookup_catalog`, de solo lectura sobre el feed
validado. La selección recibe candidatos previamente filtrados; el modelo no puede invocar
aprobación, completion ni proveedor de pagos. La pausa de LangGraph se reanuda solamente después de
una aprobación registrada en SQLite. El presupuesto se verifica contra el checkout del merchant.

## Pendiente antes de publicar para compradores externos

1. **Autenticación y autorización (crítico).** `POST /assistant`, `POST /conversation/turn` y las rutas
   `/purchases/{run_id}` no identifican al comprador. `GET /purchases/{run_id}/approval` puede
   revelar términos con datos personales; quien conozca un `run_id` puede aprobar, cancelar o
   intentar completar. CORS y la huella de aprobación no resuelven esto. Integrar un proveedor de
   identidad, validar tokens en FastAPI, derivar `principal_id` del token y asociar/verificar dueño
   para conversaciones, turnos y compras. Mantener el webhook del merchant bajo su HMAC separado.
2. **Recuperación de la UI (alto).** Una recarga crea otra conversación y pierde la tarjeta de una
   compra pendiente; el backend bloquea nuevas compras, pero no devuelve un bootstrap suficiente
   para restaurar la aprobación. Diseñar un endpoint de compras pendientes *por usuario* y rehidratar
   la tarjeta solamente después de implementar propiedad/autorización. Cubrir también la desconexión
   entre preparar checkout y registrar la tarjeta.
3. **Pendientes expirados (medio).** Una sesión expirada puede seguir ocupando el guard local.
   Conciliar su estado con el merchant y cerrarla sin inferir aprobación ni éxito de pago.
4. **Búsqueda de feeds grandes (medio).** El lookup está acotado a diez resultados y marca su
   fallback como muestra; no garantiza relevancia cuando no hay tokens compartidos. Evaluar un
   índice o búsqueda semántica solo cuando haya un feed real que lo exija, con pruebas de precisión.

La arquitectura de autenticación sugerida para una demo pública con Better Auth es: UI con sesión
Better Auth, [token JWT para servicios externos](https://better-auth.com/docs/plugins/jwt) de corta
duración, verificación de firma/issuer/audience vía JWKS en FastAPI y propietario persistido en las
compras. Better Auth requiere un [servidor de autenticación](https://better-auth.com/docs/installation)
y almacenamiento propios; no basta con instalar su cliente React. No se implementó en esta pasada
porque necesita definir ese servicio, su despliegue y la migración de datos/propiedad. Hasta entonces,
la UI y la API deben mantenerse en una demo local controlada.
