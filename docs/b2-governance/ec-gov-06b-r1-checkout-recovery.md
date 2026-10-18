# EC-GOV-06B-R1 — Recuperación de creación ACP

Dictamen: **recuperación parcialmente verificada; BLOCKED para despacho enforce comercial**.
El replay funciona con catálogo estable y credenciales válidas del mismo episodio.
No es una garantía suficiente ante cambios de catálogo. No se modifica EC-GOV-06B.

## A. Contrato observado

Merchant inspeccionado: `main`, `70ce92b97cee3d7aabf8d287f0bf700816dc584b`.
Todas las referencias locales están contenidas en main (`rev-list --all --not main --count = 0`).
No se hizo fetch: no se afirma conocer cambios remotos posteriores.

En `src/commerce_lab/checkout.py`, `CheckoutService.prepare` verifica primero
el episodio y busca `(run_id, actor_id, SHA256(K))`. El fingerprint cubre el
input interno completo, excepto K: oferta, revisión esperada, cantidad y contexto
de entrega. La misma clave con fingerprint diferente devuelve conflicto.
`create_snapshot` conserva el Checkout interno original, separado del snapshot
mutable. La restricción UNIQUE, `ON CONFLICT` y la segunda lectura serializan
creaciones concurrentes; esta propiedad se probó con cuatro solicitudes.

Pero `ACPCheckoutAdapter.create`, en `src/commerce_lab/acp.py`, consulta primero
la oferta actual y deriva de ella revisión y contexto antes de llamar a prepare.
Por tanto, el mismo cuerpo ACP puede producir otro fingerprint interno. Además,
`_response` combina Checkout original con oferta actual (por ejemplo, `name`)
y capacidades configuradas: `create_snapshot` no guarda una respuesta ACP completa.
No hay lookup público de creación por clave. GET exige conocer checkout_id y también
vuelve a consultar la oferta actual.

## B–D. Ensayos y resultados

Ejecución real en Windows, Python 3.12.6, PostgreSQL 17.2 instalado localmente.
Docker no tenía daemon disponible; `127.0.0.1:55433` no respondía. Se creó un cluster
exclusivo en `.local/gov06br1/pgdata`, puerto libre 58227, solo loopback, con rol
sintético y base nueva `tesis_lab`. El probe verifica `SHOW data_directory` antes
de migrar o sembrar. Se aplicaron migraciones existentes exclusivamente allí;
los episodios sintéticos usan variante B2. No se abrió ninguna base operativa.
Autenticación trust solo en el cluster temporal; quedó detenido al terminar.

El servicio HTTP se ejecutó mediante FastAPI TestClient/ASGI y PostgreSQL real.
La pérdida de respuesta se simula descartando el resultado después del commit;
no se inyectó una rotura de socket TCP. El ensayo de reinicio usa dos procesos
Python Merchant independientes y el mismo cluster. No se probó un reinicio del
servidor PostgreSQL, corte eléctrico ni persistencia tras fallo del sistema operativo.

| Escenario | Resultado comprobado |
| --- | --- |
| Respuesta descartada, cliente nuevo, mismo K/cuerpo/contexto | 201; respuesta JSON idéntica; exactamente un checkout |
| Dos procesos Merchant independientes | 201; mismo JSON e ID; exactamente un checkout |
| Solo revisión +1 | 422 `idempotency_conflict`; un checkout existente |
| Precio distinto, misma revisión | 201; precio original y JSON original |
| Sin stock, misma revisión | 201; mismo JSON original; no crea otra sesión |
| Precio o stock distinto y revisión +1 | Ambos: 422 `idempotency_conflict` |
| Nombre distinto, misma revisión | 201; mismo ID, distinto JSON ACP |
| Desaparición física tras crear | DELETE rechazado por FK; replay posterior 201 |
| Mismo K, variante existente distinta | 422 `idempotency_conflict` |
| Otro episodio con mismo actor, y otro episodio/actor | Crea sesiones independientes; GET de la original devuelve 404 |
| Token expirado / revocado | Ambos: 401; token nuevo para el mismo run/actor recupera original |
| Cuatro POST concurrentes con K | Cuatro 201; un solo ID y una fila |
| Mismo Request-Id enviado dos veces | Merchant genera dos IDs distintos; observaciones correlacionan el mismo checkout |

La mutación sin incremento de revisión es un ensayo diferencial SQL sobre datos
sintéticos; no demuestra que el publicador normal permita esa mutación. Con incremento
de revisión sí queda demostrado el bloqueo del replay. La baja lógica por stock se
probó; una oferta ausente en `_offer` produce `OFFER_NOT_FOUND` por inspección, pero
no se fabricó una eliminación violando la FK para atribuirle evidencia dinámica.

Probe Merchant: **15 passed, 2 warnings, 20.46 s**, cero omitidas.
Las advertencias son deprecaciones Starlette/httpx y anyio BlockingPortal.
Antes hubo dos corridas de **15 failed, 2 warnings**, en 19.86 s y 19.44 s:
el cluster heredó America/Bogota y `_as_timestamp` no convierte a UTC. Un intento
intermedio de configurar UTC fue rechazado por permisos del sandbox; no había
aplicado el ajuste cuando comenzó la segunda corrida. Tras `ALTER DATABASE ... SET
timezone TO 'UTC'` en la base temporal, la matriz pasó. No se cambió código ni se
presentan esas corridas fallidas como pruebas exitosas de idempotencia. El puerto
inicial 55449 tampoco pudo abrirse; Windows asignó después el puerto libre indicado.

Evidencia local reproducible (no versionada):

- `.local/gov06br1/probe_merchant.py`: matriz y worker de proceso nuevo.
- `.local/gov06br1/merchant-tests.txt`, `merchant-tests-utc.txt`,
  `merchant-tests-final.txt`: corridas completas.
- `.local/gov06br1/probe_agent.py`, `agent-wire.json`: transporte capturado sin red.
- `.local/gov06br1/before.json`, `seven-hashes.json`: preservación SHA-256.

Comando Merchant, desde su repositorio, sin cargar `.env`:

```text
.venv/Scripts/python.exe -B -m pytest C:/ANDES/Tesis/tesis-commerce-agent/.local/gov06br1/probe_merchant.py -v -p no:cacheprovider --tb=short --basetemp=C:/ANDES/Tesis/tesis-commerce-agent/.local/gov06br1/pytest-merchant-final
```

Para repetir es necesario arrancar expresamente el cluster temporal conservado,
mantener su puerto en `port.txt` y UTC en su base. El probe no es un entrypoint del
agente y se ejecuta con el Python del Merchant: no se añade una dependencia entre
paquetes productivos. Ningún archivo del Merchant se escribió para estos ensayos.

En dos procesos independientes del agente se ejecutó `ACPClient.create_checkout`
con MockTransport que captura el cuerpo y lanza ReadTimeout. Ambos emitieron:

```json
{"line_items":[{"id":"Q100286751-256GB"}],"currency":"cop","capabilities":{"payment":{"handlers":[]}}}
```

SHA-256 de ambos cuerpos: `6154d64ae4906f2db57e15f2a130da401aba48e2842abd0c768943c8e8547798`.
Método/ruta/API-Version y hash de K coinciden; Request-Id cambia; un intento por
proceso. Es reproducción real de serialización y simulación del transporte,
no una recuperación integrada Agent–Merchant ni prueba de binding de credenciales.

Validación del agente ejecutada nuevamente en R1 (no son resultados reutilizados
del informe 06B), con UV_NO_SYNC/UV_OFFLINE, temporales aislados, trazado desactivado
y flags de E2E/LLM/DB externa deshabilitados:

| Control | Resultado |
| --- | --- |
| `uv run ruff format --check .` | 142 files already formatted |
| `uv run ruff check .` | All checks passed! |
| `uv run pyright` | 0 errors, 0 warnings, 0 informations |
| `uv run pytest` | 618 passed, 3 skipped, 1 warning; 189.15 s |
| `git diff --check` | Correcto; documentos nuevos revisados además con `git diff --no-index --check -- NUL <ruta>` |

Los 42 casos de `tests/governance/test_enforce_integration.py` pasaron dentro de esa
suite. Las tres omisiones son E2E AP2 joint, conversational y joint, requieren servicios
externos al probe. Advertencia: alias anyio BlockingPortal de Starlette. El log completo
está en `.local/gov06br1/agent-tests.txt`. Git avisa conversión futura LF→CRLF en archivos
no congelados; no se aplicó esa conversión ni se cambió la configuración Git.
No se declara validación Linux ni integración comercial Agent–Merchant.

## C. Identidad y revisión de EC-GOV-06B

`context.py` resuelve el bearer a run_id/actor_id mediante `lab_sessions`, exige
no revocado y expiración superior a now(), y lo une al episodio. Renovar para ese
mismo par conserva el ámbito de K; cambiar de episodio puede crear otra operación
con el mismo K. La validez del token usa tiempo real, no el reloj del escenario.

`api.py` genera Request-Id del servidor; `request_observability.py` registra ID,
run/actor, hash de K e ID de checkout cuando está disponible. No hay endpoint de
recuperación por Request-Id. Si se pierde la respuesta puede perderse también ese
ID; la observabilidad puede ayudar a una investigación autorizada del Merchant,
pero no constituye un protocolo de recuperación del agente.

| Garantía local 06B | Evaluación |
| --- | --- |
| Identidad estable | Sí: operation_id, K, run local, variante, moneda, evidencia y sus digests están ligados en SQLite |
| Cuerpo reproducible | Sí para versión actual de ACPClient con los mismos argumentos; no hay envelope wire durable versionado que garantice futuros upgrades |
| Binding de autenticación Merchant | Incompleto: merchant_ref liga origen configurado, no demuestra run/actor remoto; gateway inyectado no se atesta contra esa configuración |
| Claim confirmado | Impide un segundo despacho local por el camino implementado; no es una transacción distribuida con el Merchant |
| Timeout/crash | Guarda uncertain o interpreta claimed sin resultado como uncertain; no libera claim ni reenvía |
| Reenvío automático | No existe; dispatch de producción ausente por defecto |
| Asociación de recuperación | Existe la cadena local operación→compra→evidencia; falta protocolo implementado para autenticar e incorporar un resultado recuperado |

La reserva no conserva token y no debe incorporarlo a evidencia pública. Un futuro
binding backend debe relacionar origen Merchant, run remoto y actor remoto con run
local, operation_id, digest de request y evidencia D1, con renovación controlada de
credenciales para el mismo ámbito. No basta comparar la variante o el precio para
probar pertenencia de un checkout. `experiment_run_id=None` en `_assemble` tampoco
acredita esa correspondencia remota.

Además, `d1_checkout_results` es append-only y admite una observación por operación;
un uncertain no puede convertirse en confirmed insertando otra fila ni actualizando
la existente. Reconciliar necesita un nuevo evento/esquema aprobado que preserve el
uncertain original y un CAS explícito desde la versión comercial correspondiente.
La ruta actual `_send` confirma desde selected; no implementa esa transición desde
uncertain. No se la ejecutó artificialmente como si existiera recuperación.

## E–H. Defecto, alternativas y decisión

Defecto confirmado con BD real: la idempotencia del contrato interno no equivale
a replay estable del POST ACP frente a cambios de revisión; tampoco garantiza
igualdad de la respuesta ACP frente a cambios de nombre sin revisión.
La dependencia UTC quedó expuesta y debe documentarse/corregirse por separado.

| Opción | Seguridad, compatibilidad y coste |
| --- | --- |
| A: repetir POST existente | Solo comprobada con condiciones estables; puede crear si cambia el ámbito autenticado. Insuficiente para activar 06B |
| B: resolver creación antes del catálogo | Recomendada para contrato de creación: conserva ruta ACP; exige fingerprint wire y respuesta durable atómicos, pruebas y revisión Merchant. Cambia replays de B0 bajo catálogo mutable, no su selección |
| C: consulta autenticada por K | Útil para reconciliar sin crear; añade superficie y contrato propios. Un resultado ausente durante una creación en curso NO autoriza otro envío |
| D: uncertain y conciliación manual | Medida vigente hasta validar B o C y el binding local. No inventa éxito ni prueba de ausencia; menor disponibilidad |

Recomendación: mantener D ahora y encargar B por separado al responsable Merchant.
No basta mover `_offer`: es necesario recordar identidad del request ACP y datos
originales de respuesta. C solo si se requiere recuperación estrictamente de lectura
o el propietario prefiere ese contrato; no se implementan ambos por defecto.

Merchant: ticket adjunto `ec-gov-06b-r1-merchant-ticket.md`. Agent: un incremento
posterior debe diseñar envelope durable, binding remoto verificado, renovación,
worker de recuperación y eventos/CAS de reconciliación, sin reescribir evidencia.
Antes de completar pagos habrá que consultar estado actual; el snapshot de creación
no demuestra vigencia ni consentimiento actual. Eso no cierra D2/D17.

Se preservaron por comparación SHA-256 los 306 archivos preexistentes, incluidos los
siete de 06B; no se modificaron código, dependencias, fixtures ni resultados históricos.
No se ejecutó baseline B0, PSP, pagos, E2E externos ni llamadas LLM. Merchant permanece
limpio en el mismo HEAD. En Agent solo se añaden este informe y el ticket; índice vacío,
misma rama y HEAD inicial. Sin commit, push, merge, rebase, PR ni activación enforce.
