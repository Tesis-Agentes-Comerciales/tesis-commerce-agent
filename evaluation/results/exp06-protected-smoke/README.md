# EXP-06 — ejecutor protegido, sin consumo autorizado

[PR #170](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/pull/170),
directamente hacia main. Incluye como antecedentes los commits intactos de #169,
que sigue abierto; no cambia sus protocolos, rúbricas, evidencia ni errores.

## Implementación y alcance

Comando manual: `uv run python -m commerce_evaluation.exp06_execution` muestra el
plan, sin iniciar servicios ni inferencias. `--execute` exige autorización vinculada
al snapshot exacto, hash histórico, commit validado, etapa smoke, ID único, máximo
USD y confirmación de proyecto/tarifa estándar. CI rechaza ese modo incluso con
recibo. No se habilita batch, HNP semántico ni CT19.

La frontera se instala antes del runtime: factory con ledger obligatorio,
constructor SDK y salida HTTP, transporte con reserva durable antes de cada intento
y retries. Se rechazan fallback, otro modelo/endpoint, clientes alternativos,
Async/Azure y transportes reemplazados. El registro de autorizaciones se comparte
entre worktrees del clon; no pretende controlar otros clones ni toda la cuenta.
`STOP` corta antes del siguiente envío. Se espera la terminación del turno antes
de observar efectos o retirar la frontera.

Se congelan los tres episodios y catálogos completos antes de cualquier petición.
Se reutilizan AuthClient/JWT/JWKS, router real, LiveStack/PurchaseLane/FaultProxy y
controles existentes. Se observa Merchant/PSP independientemente. Checkouts
inesperados se conservan como fallo y luego se cancelan. Bases privadas temporales
se eliminan tras detener servicios. No se envía el oráculo al Agent y no existe
calificación semántica automática: salida admisible queda pending_review.

## Primera validación, conservada

[Run 41948560226](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41948560226),
SHA `074f49ccf01823862812395aa0bc39ea0e6e9c18`: control de infraestructura
**1 passed, 0 failed/error/skipped**, `validated=true`, sin atribución P0.
Ejercitó el orquestador completo con seis errores 503 de ModelServer, tres JWT
autenticados y tres filas model_requests con un retry cada una. UX01/UX11/UX12:
**not_executed semánticamente**. No son tres escenarios aprobados.

Pines comprobados: Merchant `abf30df3b6a611284dedcc26c00accac6a853329`, PSP
`e6ab67b5963434a9a3caae2012b70232a3cca65d`. Cero efectos comerciales observados,
catálogos equivalentes, limpieza de servicios/cluster y eliminación privada
verificadas. Seis reservas simuladas desconocidas USD 2,670, conciliadas; no son
consumo real. Sin inferencias al proveedor ni pagos.

Artefacto 13379159313 archivado íntegro en `runs/41948560226.zip`:
SHA-256 `a2d6ce798f12acc77da6acaf97edc377c900a1b9959bc252ba68d3608e62bb22`,
digest de GitHub coincidente, **26 hashes internos verificados**, ninguna base
SQLite incluida. Escaneo JWT/clave privada/API sin coincidencias; no es una prueba
exhaustiva de ausencia de secretos. `first-run-integrity.json` conserva el chequeo.

[CI general 41948553378](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41948553378)
del mismo candidato terminó satisfactorio. El candidato posterior añade reclamación
compartida, STOP, manifiesto, clasificación de procedencia, JUnit explícito y corte
con exit 2; se valida de nuevo. Sus enlaces y estado final quedan en el PR.

## Cuenta y presupuesto

GET de modelo exacto devolvió HTTP 200 con la credencial del entorno: visibilidad
confirmada, **ninguna inferencia**. La consulta previa con red restringida produjo
ConnectError; se conservan ambos estados en account-checks.json. Project/organization
no están explícitos. Saldo, permisos/cuota de Responses, tarifa del proyecto y
límites de facturación no están verificados. No se mostró ni publicó la clave.

Precio oficial consultado: USD 0,25/M entrada y USD 2/M salida; snapshot deprecated,
retirada 2026-12-11. No se sustituyó. El SDK omite service_tier y auto depende del
proyecto: por eso la confirmación de tarifa estándar es requisito ANTES del consumo.
No se modifica el payload histórico para asumir otra configuración.

| Concepto del smoke HP, 3 episodios | USD |
| --- | ---: |
| Cota teórica conservadora a tarifa estándar (18 intentos) | 6,408 |
| Reserva con 25% de margen, USD 0,445/intento | 8,010 |
| Máximo propuesto, todavía no autorizado | 9,00 |

El límite efectivo será el autorizado (hasta USD 9), mostrado antes de ejecutar y
registrado en manifiesto. Si no cabe una reserva completa, no se envía. Timeouts,
respuestas perdidas, error o usage ausente conservan reserva. El reporte concilia
intentos/ledger, coste conocido y reservas desconocidas; la factura del proyecto
se concilia aparte. No se presenta la reserva como cobro ni como tope global de cuenta.

## Pendientes para consumo

Revisar el nuevo candidato y su evidencia, confirmar proyecto/tarifa estándar y
vigencia/precio al autorizar, aprobar explícitamente snapshot y máximo USD del
smoke y generar el recibo privado referido al commit validado. Se mantiene el
contexto histórico America/Bogota / viernes 2026-11-06 y el hash original
`bd813f2541546ddacb1bfd27fdd23f785477ecb95454fdf394b10e56491f2d2b`.
Los revisores humanos aplicarán la rúbrica congelada después; no se cambia por las
respuestas. La ejecución real y su validación semántica permanecen BLOCKED.

Operación y recibo no habilitante: `docs/design/exp-06-protected-smoke.md`.
No se ha escrito una autorización real, consumido API de inferencia ni hecho merge.
