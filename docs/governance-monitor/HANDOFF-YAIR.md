# Handoff para Yair — MON-02A/B

Estado: **PARTIAL**, rama `codex/hnp-ui-readonly-monitor`, repositorio `Tesis-Agentes-Comerciales/tesis-commerce-agent`; base `83510f8dd5fbe5026d909df2b4949802b8b2aa1a`.

La URL real del PR draft y el SHA publicado quedan en la entrega de publicación y en la cabecera del PR. Desde el clon, `git rev-parse HEAD` identifica el commit reproducido; no se incluye un SHA autorreferente inventado en este documento. Consultar el PR abierto de esta rama antes de comenzar y no confundir la base histórica con el commit del monitor.

## Instalación, inicio y detención

Seguir [README.md](README.md) desde un clon de esta rama: Node 24, npm con el lockfile existente, Python 3.12 para cinco tests. No copiar .env ni solicitar credenciales. Preparar la fixture, construir UI, iniciar el lector con `governance_snapshot_demo.ps1 -Action Start` y el preview con la configuración publicada. Abrir http://127.0.0.1:5182/governance. Detener UI con Ctrl+C y lector con `governance_snapshot_demo.ps1 -Action Stop` desde la raíz.

Reproducción offline es el modo inicial; Snapshot backend local hace un GET a una fixture histórica fija, sin autenticación real. El retorno offline es explícito. Para probar desconexión, detener solo el lector y pulsar Releer snapshot: última lectura obsoleta o fuente desconectada según hubiera una lectura válida anterior. No inicia nuevas compras.

## Qué se adelantó

MON-02A separa ejecución, creación, recuperación y efectos, conserva datos ausentes y distingue presupuesto del validador HNP frente al backend. MON-02B añade lectura de fixtures sanitizadas por HTTP loopback con hash y contrato, desconexión y fallback explícito. Cuatro recorridos y 69 tests; no se introdujeron dependencias ni cambios de políticas.

HNP-01 es guionado con roles programados; backend experimental, executed_policy_ids=[] y sink ficticio. HNP-02 con dos LLM reales sigue pendiente: no hay independencia experimental demostrada, presupuesto aprobado, credenciales habilitadas ni autorización de consumo.

## Continuación aislada y revisión

Crear una rama propia desde el SHA revisado. Se puede continuar accesibilidad, adaptación visual y tests herméticos del monitor sin tocar contratos ni evidencias. Coordinar cambios al punto de entrada ui/src/main.tsx; el runtime SSE y App no se modificaron. No usar Lab #19 ni ramas históricas como base ni hacer cherry-pick automático de HNP-01.

Antes de fusionar: revisar tipos del esquema v2, semántica de incertidumbre, procedencia de las cinco fuentes, atributos de bytes, exclusión del servicio en producción, lista fija de IDs y CI ordinario. Las pruebas de navegación usan dobles y la revisión del preview no equivale a una prueba comercial completa.

MON-02C requiere diseño separado: autenticación real, ownership por ejecución/operación, permisos de lectura, contrato de eventos con cursores, redacción del servidor y pruebas de ausencia de mutación. No conectar el SSE comercial por conveniencia ni implementar este incremento sin autorización.

HNP-02 necesita oráculos preregistrados, roles/proveedores independientes, control de costos y credenciales por rol, aislamiento del sink y prohibición verificable de checkout/pagos. Preparar propuesta antes de ejecutar proveedores; ningún modelo está autorizado por esta publicación.

Permanecen PARTIAL, I10/D08/D09 parciales y AUDIT-MERCHANT-CANCEL-01 con dos ejecuciones y gate sin aprobar. No fusionar los PR documentales ni modificar Notion como parte de este handoff.
