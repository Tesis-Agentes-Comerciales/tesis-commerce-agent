# EXP-06 — ejecutor manual protegido (ampliación independiente de #169)

Alcance autorizado: implementar y validar sin inferencias pagadas. No cambia
`exp06-hp.json` (SHA-256 `bd813f2541546ddacb1bfd27fdd23f785477ecb95454fdf394b10e56491f2d2b`),
rúbricas, productos ni resultados anteriores. El nuevo PR contiene los commits
originales de #169 porque siguen fuera de main; su base de revisión es main.

## Hallazgo y decisión

La guarda de #169 se instalaba sólo en su prueba, no en un ejecutor manual.
`ModelRequestRecorder.http_client` es fail-open y puede devolver None; entonces
OpenAI construye otro cliente sin hooks. Router, chooser e intent reviewer usan
ese factory. Esto impide afirmar que la preparación anterior protegía cualquier
ejecución real. Se conserva ese código histórico y se añade una frontera del
ejecutor: factory obligatorio con ledger y modelo exacto, transporte envuelto,
constructor OpenAI que rechaza clientes alternativos y límite de salida HTTP.
Async/Azure, endpoints externos y clientes sin guarda se rechazan. La frontera
se instala antes de construir el runtime y se mantiene hasta detenerlo.

El transporte reserva USD 0,445 antes de enviar, por intento real del SDK, y
persiste el evento con fsync. Conserva reservas desconocidas; las concilia con
usage del proveedor y model_requests sin descontar caché ni duplicar razonamiento.
Errores de escritura impiden enviar. Un rechazo de presupuesto enclava el corte;
el retry interno tampoco puede cruzarlo. La autorización vincula snapshot, hash
histórico, commit del ejecutor, etapa smoke, ID único y máximo USD (hasta 9).
Un archivo de reclamación exclusivo impide reiniciar la misma autorización en
los worktrees de este clon (registro privado en el directorio Git común). No es un control de gasto de otros procesos ni de toda la cuenta.

## Secuencia

El comando por defecto sólo muestra el plan. `--execute` exige recibo humano;
se rechaza en CI general/nightly. La excepción solicitada posteriormente por el
usuario es el workflow manual protegido exp06-smoke.yml, con alcance explícito. No hay opción HNP/CT19; el lote solo es local (ver «Lote»). Antes de crear
servicios comprueba repositorios limpios y SHAs hermanos exactos. Reutiliza
LiveStack/PurchaseLane, AuthClient/JWT/JWKS, create_app y el router de producto.
Congela los tres episodios, catálogos completos, contexto y rúbricas antes de
la primera solicitud. Cada episodio tiene runtime, comprador y base Agent propios.

Cada turno se envía por el adaptador autenticado existente. Se conservan salida,
ledger sanitizado, hashes, tiempo, usage, reservas y snapshots independientes de
Merchant/PSP. Una respuesta válida sólo queda pending_review: no hay calificador
semántico automático. Un error detiene el smoke real; casos restantes se registran
not_executed. Los checkouts inesperados se conservan como fallo antes de cancelar;
no se llama a aprobación ni complete. Se verifica limpieza y se emite JUnit técnico,
sin presentar pending_review como un PASS semántico.

## Cuenta y límites

Modelo exacto deprecated, retirada documentada 2026-12-11. Precio oficial consultado:
[USD 0,25/M entrada y USD 2/M salida](https://developers.openai.com/api/docs/models/gpt-5-mini).
[Vigencia](https://developers.openai.com/api/docs/deprecations).
Máximo teórico conservador: 18 intentos × (400000×0,25 + 128000×2)/1M = USD 6,408;
reserva con margen del 25% = USD 8,010. Límite efectivo: el menor entre autorización
y USD 9; se rechaza ANTES del intento si no cabe la reserva completa. No son costes
esperados ni una autorización. Precio/vigencia deben revalidarse al autorizar.

`--check-account` sólo realiza GET del modelo, con credencial del entorno: no
inferencia ni sustitución. Registra presencia de project/organization, nunca sus
valores ni la clave. Un GET exitoso no acredita saldo, permisos Responses o límite
duro del proyecto. La facturación final requiere conciliación posterior con el
dashboard/usage del proyecto; sin acceso administrativo se declara no verificada.
El usuario debe confirmar proyecto de facturación y límite; no se cambian ajustes.

Consulta realizada desde este entorno: GET del snapshot exacto HTTP 200, credencial
presente, project/organization no explícitos. El primer intento en red restringida
dio ConnectError; la consulta con acceso de red autorizado sí obtuvo 200. Ninguna
consulta creó una respuesta de modelo. Saldo, cuota de Responses y dashboard de
facturación permanecen no verificados.

## Operación manual después de autorización (no ejecutada aquí)

El arnés de servicios usa pass_fds y grupos de procesos POSIX: la ejecución manual
requiere un host POSIX, Linux o macOS. Windows no está soportado y se rechaza antes de
reclamar el recibo (`require_live_host()` exige `os.name == "posix"`; el arnés lo documenta en
`tests/integration/README.md`). Las validaciones de Actions usan su ubuntu-24.04 ya preparado.
El smoke autorizado del 2026-11-05 corrió en macOS (`evaluation/results/exp06-smoke/`).
En la etapa anterior, el host disponible era Windows con Ubuntu WSL, sin uv/Node ni integración
Docker; no se instaló infraestructura ni se cambiaron ajustes de Docker.

Desde el checkout validado, con Python/Node y dependencias congeladas de los tres
repositorios, exportar MERCHANT_REPO/PAYMENT_REPO hacia los SHAs fijados. La credencial
existente sólo se lee del entorno, nunca de prompts ni argumentos de línea de comandos.

```sh
uv run python -m commerce_evaluation.exp06_execution
uv run python -m commerce_evaluation.exp06_execution --check-account
# Sólo el usuario, tras aprobar snapshot, recibo y máximo USD:
uv run python -m commerce_evaluation.exp06_execution --execute \
  --authorization .local/exp06-approval.json --output .local/exp06-smoke-01
```

El recibo privado se prepara DESPUÉS de la autorización humana, con este esquema:

```json
{
  "authorized": false,
  "authorization_id": "ID_UNICO_DE_LA_APROBACION",
  "human_reference": "REFERENCIA_A_LA_AUTORIZACION_EXPLICITA",
  "executor_sha": "SHA_COMPLETO_VALIDADO_DEL_NUEVO_PR",
  "model": "gpt-5-mini-2025-08-07",
  "protocol_sha256": "bd813f2541546ddacb1bfd27fdd23f785477ecb95454fdf394b10e56491f2d2b",
  "stage": "smoke",
  "max_usd": "9.00",
  "billing_project_confirmed": false,
  "standard_pricing_confirmed": false
}
```

El producto omite service_tier y la API podría heredar una tarifa del proyecto.
El adaptador de evaluación fija ahora service_tier=default mediante el SDK y lo
exige en el transporte antes de enviar. Se registra ese parámetro y el tier de
respuesta; una respuesta no estándar o desconocida enclava el corte sin liberar
reserva. Es una condición técnica nueva declarada, sin cambiar producto, mensajes,
modelo, límites de salida, protocolo histórico o rúbricas. Permite usar la tarifa
estándar documentada sin modificar ajustes de la cuenta. El recibo confirma las
tarifas, no presupone una configuración administrativa desconocida. El tope no
controla otros procesos ni el gasto global de la cuenta.

Estos valores false son deliberados: el ejemplo no habilita consumo. El recibo
vincula el commit validado y exige confirmación humana del proyecto de facturación,
incluso si la clave ya está asociada implícitamente a un proyecto. Reutilizar su ID
se rechaza aunque se elija otro output. Crear el archivo `STOP` dentro de la carpeta evidence impide el siguiente intento;
un intento ya enviado debe finalizar o agotar su timeout y conserva su reserva.
Tras un corte no se relanza: se revisa journal,
ledger y gasto del proyecto; cualquier continuación requiere nueva decisión humana.
La reserva persistida es una cota contable local, no una conciliación de factura.
El reporte distingue uso conocido, reservas desconocidas y conciliación pendiente.
Las bases privadas son temporales y se eliminan después de detener servicios;
sólo evidencia y logs sanitizados son publicables.

## Lote (`--stage batch`)

Añadido el 2026-11-05, tras el smoke y con la autorización de Yair para el lote. Es el mismo
ejecutor; solo cambian los límites, que se leen de `exp06-hp.json` y no se reescriben:

| Etapa | Episodios | Intentos máximos | Reserva máxima | Tope máximo del recibo |
| --- | ---: | ---: | ---: | ---: |
| `smoke` (por defecto) | 3 | 18 | USD 8,010 | USD 9,00 |
| `batch` | 15 | 90 | USD 40,050 | USD 45,00 |

- **Orden.** Repetición afuera, UX01/UX11/UX12 adentro (`design.order`): `UX01-r1`, `UX11-r1`,
  `UX12-r1`, `UX01-r2`… `UX12-r5`. El smoke conserva los nombres `UX01`, `UX11` y `UX12`.
- **Episodios.** Cada uno es nuevo, con runtime, comprador y base del Agent propios. Los 15
  se congelan antes de la primera solicitud. Sus archivos llevan el sufijo: `UX01-r1-frozen.json`,
  `-observed.json` y `-review.json`. El `turn_id` no llega al modelo.
- **Recibo.** `stage` del recibo y `--stage` deben coincidir. El tope del recibo no puede pasar
  de `proposed_batch_cap_usd`. La reclamación guarda la etapa y la guarda la vuelve a comprobar
  antes de cada intento. Un recibo de smoke no abre un lote, ni al revés.
- **Actions.** El workflow sigue siendo solo smoke: `matches_dispatch` solo produce recibos
  `smoke`, así que un lote se rechaza en CI.
- **Parada.** Se conserva la regla del smoke: el primer episodio que no quede `pending_review`
  detiene el lote, y los siguientes quedan `not_executed`. El prerregistro no fija otra regla
  para el lote. Continuar exige una decisión humana nueva.

```sh
uv run python -m commerce_evaluation.exp06_execution --stage batch
uv run python -m commerce_evaluation.exp06_execution --execute --stage batch \
  --authorization .local/exp06-batch-approval.json --output .local/exp06-batch
```

## Validación previa

Tests offline: ausencia/autorización incorrecta, snapshot/hash distintos, agotamiento,
timeouts/usage ausente, replay, clientes alternativos, retries y fallo de ledger.
Control live sin proveedor: mismo orquestador, tres turnos con ModelServer de errores,
servicios reales fijados, PostgreSQL desechable, JUnit y limpieza. No acredita
comprensión semántica. Ningún workflow recibe claves de modelo o habilita pagos.

## Ejecución manual en GitHub Actions (preferencia confirmada por el usuario)

No se instala nada en Ubuntu local. El pedido explícito del usuario permite
preparar consumo API en un workflow manual protegido, como excepción acotada a la
regla general de AGENTS.md sobre CI. La prohibición sigue vigente para ci.yml,
pruebas, PR/push y nightly programado. Todavía no hay autorización de consumo.

`.github/workflows/exp06-smoke.yml` tiene workflow_dispatch y una entrada reusable
EXCLUSIVAMENTE de validación: workflow_call no admite mode ni secretos de modelo.
El único llamador añadido al nightly exige workflow_dispatch con scope
exp06-actions-validation y pasa sólo el token de lectura de hermanos. Nunca puede
lanzar smoke pagado; el resto del nightly conserva sus condiciones y pines.

GitHub requiere el archivo en la rama predeterminada para mostrar Run workflow.
Por eso se valida antes de fusionar con ese puente, reutilizando el mismo YAML de
setup/validación en Actions. Después de revisión e integración humana se podrá
lanzar directamente desde Actions > EXP-06 protected manual smoke > Run workflow.
No se realiza merge en esta unidad.

Modo por defecto validate, presupuesto 0, confirmaciones false y autorización
vacía: sólo ModelServer. El modo smoke requiere el SHA completo revisado igual al
ref seleccionado, snapshot exacto, máximo USD positivo <=9, confirmaciones de
proyecto/tarifas y texto AUTHORIZE_EXP06_SMOKE. Se valida contra el evento original,
repositorio y archivo workflow exactos, run ID y run_attempt=1. Un rerun nunca puede
reutilizar la autorización en otro runner. Una continuación exige nuevo dispatch
y revisión del gasto anterior. No hay batch ni HNP.

Secretos en Settings > Secrets and variables > Actions:
- EXP06_OPENAI_API_KEY (obligatorio para smoke; actualmente no está configurado).
- EXP06_OPENAI_PROJECT_ID y EXP06_OPENAI_ORG_ID si corresponden al proyecto elegido.
- MERCHANT_REPO_TOKEN existente: lectura de los hermanos fijados.

No se copian claves locales a GitHub. Los secretos del proveedor se inyectan sólo
en el paso de ejecución, después de verificar el alcance; no pasan a validación,
prompts, receipt, logs ni bases archivadas. Artefactos incluyen sólo evidencia
pública, sin receipt ni bases privadas. El gate técnico conserva failed/error/blocked
y not_executed y nunca declara PASS semántico. Ante cancelación con evidencia
incompleta se conserva incertidumbre, no se relanza y se concilia con el proyecto.
El coste observado por tokens (caché al precio completo) es una cota: la factura
requiere revisar la ventana/modelo/proyecto, evitando otro tráfico o declarando
que no es atribuible. No se afirma haber conciliado una factura inexistente.
