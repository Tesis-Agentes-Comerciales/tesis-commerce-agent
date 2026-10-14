# Modelo de amenazas B2

## Activos

- restricciones e intención del comprador;
- identidad y consentimiento humano;
- selección, términos del checkout y revisión de la oferta;
- mandates/recibos AP2 y token opaco de pago;
- bundle de política y evidencia de decisiones;
- inventario, orden e idempotencia.

## Adversarios y fallos considerados

- vendedor o catálogo que publica texto malicioso;
- usuario que intenta escalar autoridad o manipular el flujo;
- respuesta de herramienta malformada, obsoleta o comprometida;
- modelo que selecciona mal, inventa datos o sigue una instrucción indirecta;
- política ausente, corrupta, indefinida o lenta;
- reintentos, carreras y replay de aprobación;
- error interno que deja una traza incompleta.

No se modela el compromiso total del host o de las claves privadas. Ese riesgo requiere controles de
infraestructura fuera del prototipo.

## Trazabilidad amenaza–control–prueba

| Amenaza | Fuente | Control B2 | Prueba |
|---|---|---|---|
| Goal hijack por contenido externo | OWASP Agentic, MITRE AML.T0093 | Datos de catálogo sin autoridad, campos permitidos, P2/P5 | Inyección directa y retrasada en descripción |
| Uso indebido de herramienta | OWASP Tool Misuse | Modelo solo posee lookup; efectos detrás de servicios deterministas | Intento de checkout/pago desde contenido o tool output |
| Abuso de identidad/privilegio | OWASP Identity & Privilege | Principal derivado del JWT; ownership en P4/P5 | Otro usuario intenta aprobar/completar el `run_id` |
| Envenenamiento de configuración | MITRE AML.T0081 | Bundle fijado por digest, readiness, default deny | Digest desconocido o bundle inválido |
| Exfiltración por tool invocation | MITRE AML.T0086 | No secretos en prompts/policy input; allowlist y redacción | Buscar bearer/payment/mandates en trazas y errores |
| Obfuscación de prompt | MITRE AML.T0068 | Enforcement independiente del texto/modelo | Payload codificado y Unicode en catálogo |
| Instrucción retardada/memoria | MITRE AML.T0094 | Historial saneado; P5 revalida el flujo completo | Inyección en turno N que intenta activarse en N+1 |
| Toxic flow entre herramientas | Invariant Labs | Decisiones acumulativas y P5 | Secuencia lookup → selección → cambio de oferta → completion |
| Oferta obsoleta / TOCTOU | B0 S04, AP2 | Relectura, revisión, expiración, invalidación y nueva aprobación | Subida/bajada de precio entre presentación y clic |
| Fallo del motor de políticas | ToolEmu / operación | Timeout estricto, esquema de salida, fallo cerrado | OPA caído, lento, 500, `undefined` o JSON inválido |
| Replay/doble efecto | AP2 y controles B0 | Fingerprint, nonce/revisión, CAS e idempotencia | Reutilizar aprobación y repetir completion |
| Evidencia incompleta | NIST/OPA | Append-only y obligación previa al efecto | Forzar fallo de SQLite al guardar decisión |
| Exceso de confianza humana | OWASP Human-Agent Trust | Términos cerrados, razón estable y rechazo visible | Verificar que candidatos no parezcan aprobación |

## Casos que siempre fallan cerrado

- política no disponible o no ready;
- versión/digest del bundle diferente del manifiesto del experimento;
- entrada o salida que no valida contra esquema;
- checkout sin revisión, expiración o firma esperada;
- principal no propietario;
- huella/revisión distinta de la presentada;
- falta de evidencia de P2, P3, P4 o P5;
- obligación desconocida o no ejecutada;
- error al persistir evidencia antes de un efecto.

## Riesgo residual explícito

B2 reduce acciones no autorizadas; no garantiza que la recomendación sea óptima, justa o libre de
sesgos comerciales. Tampoco prueba que las reglas elegidas sean normativamente correctas. Para no
ocultar ese límite, la evaluación reporta por separado seguridad, utilidad, falsos bloqueos,
explicabilidad y latencia.
