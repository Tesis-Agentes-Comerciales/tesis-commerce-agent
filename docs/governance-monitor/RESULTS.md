# Resultados técnicos — MON-02A/B

Corte de publicación: 19 de octubre de 2026. **PARTIAL**, base organizacional `83510f8dd5fbe5026d909df2b4949802b8b2aa1a`. Estos resultados validan un monitor de reproducción y un GET local de fixtures; no una compra gobernada completa.

## Comprobaciones ejecutadas

| Grupo | Tests distintos | Resultado |
|---|---:|---|
| Adaptador, compatibilidad histórica, invariantes y render SSR | 30 | PASS |
| Contrato/lector/estado de fuente y servidor HTTP de fixtures | 26 | PASS |
| Selección hermética de rutas con módulos dobles | 8 | PASS |
| Integridad y proyección de artefactos Python | 5 | PASS |
| **Total sin contar repeticiones** | **69** | **PASS, cero omitidos** |

Oxlint, build TypeScript/Vite y comprobación sintáctica de los dos scripts Node: PASS. Ruff format/check de los dos archivos Python publicables y Pyright enfocado: PASS, cero errores. Node 24.15.0, npm 11.12.1 y Python 3.12; comandos ejecutados mediante Windows PowerShell 5.1. Sin nuevas dependencias ni modificaciones al lockfile.

El primer intento de los tests HTTP encontró EACCES del sandbox en sockets loopback; se conserva como intento bloqueado, no como defecto del monitor resuelto por cambiar assertions. Se repitieron únicamente los 26 tests afectados con acceso al transporte local autorizado y aprobaron. No se suman como tests nuevos. Los 20 requests reales registrados apuntaron exclusivamente al listener de fixtures de prueba: seis métodos rechazados, dos archivos alterados/sobredimensionados rechazados, cero requests comerciales y cero remotos. Es un registro de ese transporte de prueba, no captura exhaustiva de red del host.

Revisión de navegador con el preview publicado: `/governance` abre offline; seleccionar Snapshot backend local lee el GET y etiqueta fixture histórica/obsoleta. `/` conserva la pantalla normal de acceso. Los ocho tests comprueban además /settings, /signin y variantes de /governance sin importar runtime comercial en el monitor. No se enviaron formularios ni se probó autenticación comercial. El preview bloquea /api/; por tanto no se afirma validación funcional de los flujos de compra. Ambos procesos de demostración se detuvieron al terminar.

## Semántica y fuentes

MON-02A corrige tres errores de presentación: badge confirmado antes de comenzar; confirmación de creación/recuperación atribuida a un bloqueo; budget pass del validador presentado como control backend aprobado. La fuente HNP contiene realmente ese pass erróneo y el backend deny por 13000 > 12500; se conservan ambos. El adaptador de compatibilidad interpreta confirmación de una observación de bloqueo como creación/recuperación no aplicables, sin reescribir el JSON histórico.

Las pruebas rechazan denegación con envío/efecto, confirmación sin inicio y recuperación contradictoria; los campos ausentes no se reconstruyen mediante IDs o digests. Se distingue HP replay HTTP/PostgreSQL histórico de HNP scripted offline con backend experimental. HNP-01 no ejecuta PDP D1, no conecta negocio y no representa dos LLM independientes.

MON-02B sirve el mismo bundle completo de cuatro escenarios, fijado por hash. Rechaza ID/ruta/método/esquema/tamaño/redacción/contradicción incompatibles; la UI cubre timeout, desconexión, snapshot obsoleto, rechazo íntegro y alternativa offline explícita. Algunos casos usan transporte HTTP real local; otros son entradas sintéticas en memoria o dobles del lector. Ninguno ejecuta Agent–Merchant, D17 o D2 nuevos.

## Integridad y alcance

41 fuentes JSON/CSV/JSONL protegidas idénticas al corte MON-02A. Custodia local: ocho worktrees históricos con 2688 archivos y 1806 archivos de Lab comprobados, originales HNP fijados y paquetes HP/B2 original/v2 intactos. Worktrees documentales preservados y sin modificaciones. Antes del stage el índice estaba vacío; después se revisa únicamente la selección del monitor. El manifiesto publicado fija los bytes de los archivos incluidos; no contiene logs ni bases operativas.

Los PR Agent #10, Merchant #2 y Lab #19 quedan fuera de este cambio. No se modifican CI, dependencias, migraciones, políticas, contratos comerciales, App, SSE ni experimentos HNP originales. Solo cambia el entrypoint existente ui/src/main.tsx; el resto del incremento es aditivo. Los checks generales del CI ordinario se ejecutan automáticamente al abrir el draft, con resultado informado por separado del corte local. No se activan integraciones opt-in manuales.

## Límites pendientes

Se mantienen PARTIAL, I10/D08/D09 parciales, limitaciones ASGI e identidad sintética y AUDIT-MERCHANT-CANCEL-01 con sus dos cancelaciones y gate histórico sin aprobar. La UI no agrega evidencia para cerrar esos gates. GET local sin autenticación, autorización de ownership, stream ni eventos actuales; exclusivamente fixture sanitaria fijada. Los hashes prueban consistencia de bytes, no firma del productor o consentimiento humano.

MON-02C (lectura autenticada) y HNP-02 (dos LLM con costos/oráculos/aislamiento aprobados) requieren otro encargo. No están implementados ni autorizados por este PR. Ver README.md y HANDOFF-YAIR.md para reproducción y continuación aislada.
