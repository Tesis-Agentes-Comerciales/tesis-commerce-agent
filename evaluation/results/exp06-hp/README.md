# EXP-06 — infraestructura HP y prerregistro semántico

[PR #169](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/pull/169),
base `eb18775e5b620436d949a82803f5415ec7455bf3` (CI postmerge 41940776855 satisfactorio).
No hay llamadas pagadas, resultados semánticos ni ejecución HNP en esta unidad.

## Resultado observado

[Actions 41943476926](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41943476926)
ejecutó `873d3019a171e84177044989f2387fbe60bb0410`:
**1 collected, 1 passed, 0 failed/error/skipped**, puerta de infraestructura
`validated=true`, sin atribución de escenarios P0. UX01/UX11/UX12 permanecen
`not_executed`. Merchant `abf30df3b6a611284dedcc26c00accac6a853329` y PSP
`e6ab67b5963434a9a3caae2012b70232a3cca65d`, ambos limpios y con locks comprobados.

Se utilizó el router de producto sin inyectar un router alternativo: el SDK recibió
dos errores 503 del `ModelServer` existente mediante `MockTransport`. Hubo una fila
`model_requests` (`model.route`, `InternalServerError`, un retry), dos intentos HTTP
con hashes registrados y HTTP 502 hacia el cliente autenticado. Los tokens reales
son desconocidos/no aplicables a una inferencia inexistente, no uso medido cero.
Llamadas al proveedor: **0**, consumo API: **USD 0**. La reserva **simulada** USD 0,890
demuestra el tratamiento conservador de errores, no un cobro.

JWT/JWKS reales del auth sintético: token ausente/inválido 401, historial autenticado
200, redirección autenticada 307 rechazada sin seguirla. El registro de identidad y
sesión sí escribe en la base auth; sus conteos y hashes se conservan separados.

Los snapshots independientes Merchant y PSP antes/después son idénticos en todas
sus tablas observadas. Cero checkouts, órdenes, intentos de completion y pagos;
compras, aprobaciones y completion_results locales también cero. `run_events` conserva
solamente `fixture.loaded`: este control no realizó `offer.read`. Catálogo completo
de 310 variantes congelado antes del turno, junto con sus metadatos. Auth y Agent
detenidos, proxy cerrado y PostgreSQL desechable eliminado.

## Integridad y trazabilidad

Artefacto original `exp06-hp-infrastructure-evidence`, ID **13378130667**, archivado
sin reempaquetar en `runs/41943476926.zip`. SHA-256:
`621c42595dd01ba521529dcdfa8bc4acaefd88a6c4e5a115561021ba9fa0841a`.
Coincide con el digest de GitHub; **15/15 hashes internos comprobados**.
`integrity.json`, JUnit, manifest, outcomes y reporte permiten revisar las afirmaciones.
El ZIP conserva catálogo, trazas y logs originales. No se publican bases SQLite,
JWT ni claves; búsqueda de patrones JWT/clave privada/API no produjo coincidencias.
Este análisis por patrones no constituye garantía universal de ausencia de secretos.

El siguiente commit `4e8f2841353353e221d895aa6e03657dafa80d0d` modifica únicamente
los tipos de los tests offline; no cambia el control live, protocolo, producto,
workflow ni condiciones del run anterior. Su
[CI general 41943618972](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41943618972)
se comprueba por separado. El estado final de los cuatro jobs del último HEAD se
registra en el PR; no se confunde con este run de infraestructura.

## Resultados desfavorables conservados

- CI inicial [41943468383](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41943468383):
  `python-lint` falló por 11 errores de tipos en tests nuevos; auth pasó y los demás
  jobs quedaron cancelados al publicarse la corrección. Log y estados en `offline/`.
  Se corrigieron las anotaciones, sin alterar aserciones ni oráculos.
- Suite completa local Windows: ocho errores de colección por `signal.SIGHUP`
  ausente en código POSIX preexistente. JUnit/log originales conservados; no se
  presenta como una suite aprobada. Linux en Actions es la validación general.
- Controles offline específicos: **20 passed** (guarda y reporte sin atribución P0).
  Ruff y Pyright de los archivos nuevos satisfactorios tras la corrección. Los
  dry-run existentes se ejecutaron sin proveedor; sus estimaciones sintéticas no
  son resultados ni costes medidos de EXP-06.

## Bloqueos y siguiente autorización

El protocolo semántico `evaluation/scenarios/exp06-hp.json` tiene SHA-256
`bd813f2541546ddacb1bfd27fdd23f785477ecb95454fdf394b10e56491f2d2b`.
Fija contexto America/Bogota y viernes 2026-11-06, rúbrica y repeticiones antes de
recibir respuestas reales. La revisión semántica es humana e independiente;
éxito estructural sólo produce `pending_review`.

Falta autorización explícita del snapshot `gpt-5-mini-2025-08-07`, etapa y máximo
USD. Propuesta: smoke de tres episodios con tope USD 9; después, sujeto a revisión,
cinco repeticiones por escenario con tope independiente USD 45. Son techos muy
conservadores, no pronósticos de consumo ni aprobaciones. La disponibilidad de la
cuenta sigue sin probarse; el snapshot está deprecated y tiene retirada documentada
para 2026-12-11. No hay sustitución automática.

Antes de consumir se debe cerrar la aceptación metodológica, comprobar vigencia y
precio, completar la orquestación manual autorizada con esta guarda instalada en
todos los clientes del recorder y verificar el acceso efectivo. Esta unidad no
habilita un ejecutor pagado. CT19 requiere su propio control de retirada/restauración
de identidad, prerregistro y PR separado; no se ejecutó aquí. No se alteran P3,
oráculos históricos, productos ni trabajo INF de Jair. No se solicita merge automático.
