# Adaptador autenticado — preparación UX01/UX11/UX12

Se extiende conversation.request_json/evaluate existentes con TokenSource compatible con
AuthClient de governance-pilot. JWT solamente en Authorization; nunca en messages/details,
resultados o excepciones. El mismo AuthClient se usa también al cancelar una preparación.
No hay fallback sin credenciales ni reintento tras fallo de autenticación. HTTP conserva
status/body sanitizado; red/error de JSON son errores técnicos, no condición comercial false.
Se rechazan redirects autenticados para impedir propagar bearer a otro destino.

Pruebas offline de headers, cancelación autenticada, reflejo de token, 401/503/302, red,
respuesta inválida y token ausente. Son bytes guionados del transporte, nunca semántica.
Validación live separada AUTH-TRANSPORT: reutiliza NodeServer/AgentServer existentes, AuthClient
signup/token real, JWKS y JwtBuyerAuthenticator de producción, LiveStack/PurchaseLane para
Merchant/PSP fijados. Sin modelos ni routers guionados. Falta/invalid token 401, JWT válido
permite GET /me/purchases, redirect 307 se rechaza. Tres textos propuestos UX01/11/12 llegan
al Agent y reciben 502 intent_model_error porque no hay modelo configurado. Esa respuesta
acredita admisión/transporte, NO comprensión. Escenarios semánticos quedan not_executed.

Se congelan entradas/protocolo antes del Agent; comprobar cero purchases/model_requests/
checkout/órdenes/pagos y Merchant/PSP idénticos. Auth sí persiste usuarios/sesiones/JWKS:
conservar conteos/huellas independientes, sin publicar base ni tokens o claves. Cerrar todos
los servicios y verificar shutdown. Evidencia usa recorded_case, redaction y reportador EXP-01;
perfil separado de CT15 y del full, sin nueva infraestructura ni cambios en ci.yml.

Para ejecución semántica futura faltan modelo/versión y presupuesto autorizados, repeticiones,
fecha/zona concreta para UX12, rúbrica congelada con revisión independiente y controles de
efectos. El CLI antiguo evaluate-conversation no se ejecuta aquí; tampoco governance-pilot
live o matriz histórica. No se reetiqueta el control de transporte como UX aprobado ni se
programan respuestas de aclaración. HP probado como transporte; HNP conversacional pendiente.
