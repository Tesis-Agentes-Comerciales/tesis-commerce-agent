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
está prohibido en GitHub Actions. No hay opción batch/HNP/CT19. Antes de crear
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

El SDK omite service_tier; la API utiliza auto y puede heredar una tarifa distinta
del proyecto. Por eso también se exige confirmación de tarifa estándar ANTES del
consumo: no se cambia el payload ni se presupone la configuración de la cuenta.
La consulta administrativa de service tiers no se ha realizado: no hay proyecto
explícito ni acceso administrativo verificado. Referencia oficial:
https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/projects/subresources/service_tier_settings/methods/retrieve
El tope financiero calculado presupone las tarifas estándar confirmadas; no limita
otros procesos, otros clones ni el gasto global de la cuenta.

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

## Validación previa

Tests offline: ausencia/autorización incorrecta, snapshot/hash distintos, agotamiento,
timeouts/usage ausente, replay, clientes alternativos, retries y fallo de ledger.
Control live sin proveedor: mismo orquestador, tres turnos con ModelServer de errores,
servicios reales fijados, PostgreSQL desechable, JUnit y limpieza. No acredita
comprensión semántica. Ningún workflow recibe claves de modelo o habilita pagos.
