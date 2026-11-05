# EXP-01 — Primer lote real, resultado parcial

Estado: **MA24 passed (HNP), INF30 passed (HP), INF31 incompleto por estímulo nativo 503**.
No es un lote completamente validado. No se modificó el producto para producir éxito.
No hay LLM, comprensión semántica, aprobación HP, pago ni orden completada.

## Fuentes y separación

- Base producto: `65183d545241937c5f741dd66378e1a7f7c83dc9`.
- Merchant: `abf30df3b6a611284dedcc26c00accac6a853329`.
- PSP: `e6ab67b5963434a9a3caae2012b70232a3cca65d`.
- Ejecución primaria: `7b531e4a0f871e5b6db4b07f991b1583ddf753ac`.
- Reanálisis de artefactos: `a1c56c612cbc97c71ed4e686942899f127407b29`.
- Oráculo/prerregistro sin cambios desde `e8b7050dad436c496798de4c590c29c3ffab02eb`:
  `evaluation/scenarios/exp01-first-batch.json`, SHA-256
  `86598a0132c75e9e72b404a6c9e53d45e1bb3ff22a27a1892c63f020ad831dfb`.

Son ejecuciones nuevas, con entrada y oráculo congelados antes del Agent. No se atribuyen
los anteriores nightly de regresión ni P3-V01 a escenarios nuevos. No depende de P2/P3.
HP usa PurchaseService, despacho runtime SingleAttempt, ACPClient real y verificador
histórico del producto; parte de un SKU de entrada real observado, sin evaluar su selección.
HNP usa create_app a través del HnpAgent existente, con propuesta/consentimiento/revocación
autenticados por las credenciales sintéticas previstas por ese arnés.

## Todos los intentos conservados

| Run | Fuente | Resultado original |
| --- | --- | --- |
| [41841403584](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41841403584) | `e8b7050dad436c496798de4c590c29c3ffab02eb` | Preflight blocked: checkout superficial impide comprobar ascendencia. Cero escenarios ejecutados. |
| [41842063615](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41842063615) | `681247a6c90c0a74d73320b8ac5fd41dc1c9619c` | 8 collected, 2 passed, 6 failed, 7 errores de teardown. Hook HP comparaba orden de claves JSON, abortando antes del reenvío; no acredita INF30/31. MA24 dejó observación, confirmada por el run siguiente. |
| [41842686257](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41842686257) | `7b531e4a0f871e5b6db4b07f991b1583ddf753ac` | **8 collected, 7 passed, 1 failed, 0 errores pytest, 0 skipped/xfail/xpass**. Puerta original false; no se ha borrado ni corregido su resultado. |

Los ZIP originales están en `runs/`, idénticos a los artefactos de Actions. SHA256SUMS y
MANIFEST.json permiten verificar integridad aunque venza la retención de Actions.
IDs de artefactos: 13328318105, 13329527280 y 13328933583, respectivamente.

## Resultados de la ejecución primaria

| Control | Observación | Dictamen |
| --- | --- | --- |
| MA24 HNP | Grant scheduled → revocación efectiva persistida → trigger 409; snapshots Merchant/PSP iguales tras revocar, sin nuevas llamadas comerciales; cero outbox/reservas/completion_results/purchases. | passed |
| INF30 HP | Merchant 201 perdido por drop; Agent d1_dispatch_uncertain; retry del mismo run exige recuperación; lookup verifica identidad/digest originales; un POST y una sesión, inspección en otro proceso sin red. | passed, integración posselección |
| INF31 proxy_503 | 503 antes de reenviar, cero aceptación Merchant; uncertain y not_found no autoriza ausencia/replay. | passed |
| INF31 timeout_before_forward | Barrera antes del reenvío; timeout del Agent; 503 posterior del proxy sin reenviar; cero sesión. | passed |
| INF31 merchant_503 | PG detenido provoca **500**, no 503. Agent registra uncertain; el estímulo requerido no se alcanzó. | error de estímulo; bloquea el cierre INF31 |
| INF31 timeout_after_accept | Merchant 201 antes del timeout; una sesión histórica recuperada, sin segunda creación ni continuación. | passed |
| INF31 definitive_stock_refusal | Merchant 409 out_of_stock; Agent failed, sin sesión; se restaura el snapshot de oferta. | passed |
| INF31 positive | Una preparación awaiting_confirmation sin aprobación ni pago; cancelación verificada. | passed |

Se cancelaron todos los checkouts creados por los controles. Los ocho registros indican
cleanup_verified=true, incluido el control fallido. Las observaciones guardan stock,
run_events, sesiones y hashes/conteos de todas las tablas Merchant; PSP se observa de forma
independiente mediante hashes/conteos, sin publicar tokens. Shutdown confirma proxy cerrado
y PostgreSQL eliminado. Los 50 hashes del manifiesto original coinciden. No hay bases privadas
ni claves en los ZIP; el escaneo de JWT/PEM/DSN no encontró coincidencias.

## Bloqueo concreto y límite de la inferencia

En Merchant fijado, `api.trusted_context` llama a `context.authenticate_lab_session`, que
consulta PostgreSQL. Con PG detenido, una psycopg.OperationalError escapa antes del handler
de preparación y produce HTTP 500. La traza de `b0/merchant.log` demuestra esa fase.
No se afirma que todo fallo de Merchant sea 500 ni que sea imposible otro estímulo 503.
Sí se demuestra que **este mecanismo nativo prerregistrado no produce el 503 requerido**.

El 503 controlado del proxy y ambos timeouts sí se ejercitaron. No se quita el control nativo,
no se acepta 500 como 503 y no se cambia el oráculo tras observarlo. Resolverlo requiere una
decisión técnica: aceptar en un protocolo futuro el 503 en la frontera de transporte, o
diseñar/revisar aparte el manejo de indisponibilidad en Merchant y fijar su nuevo SHA.
Ninguna alternativa se implementó ni quedó aprobada experimentalmente aquí.

## Reanálisis independiente, sin nueva ejecución

La puerta original trataba `incomplete_or_failed` del recorder como incompletitud incluso
con ocho casos íntegros y un fallo conocido. El código a1c56c6 conserva ese fallo, reconoce
la ejecución terminal con exit 1 y permite atribuir avances independientes; no vuelve verde
el lote. También exige observaciones independientes y rechaza cambios de oráculo/skips.
Cuatro pruebas offline del reportador pasan; no son escenarios ni datos comerciales.

`analysis/report.json` es una derivación identificada del ZIP original, no un reemplazo:
validated=false, issues=[], MA24 passed, INF30 passed, INF31 error. Inventario mínimo:
**2 passed, 1 error de ejecución/estímulo y 34 not_executed**. INF30/31 HNP y la UX pública
completa de recuperación siguen pendientes; no se declara cobertura de todos sus modos.
La proyección P0 se muestra aparte porque su contrato no representa blocked/error.

Para reproducir el reanálisis sin Agent, extraer el ZIP primario en una carpeta nueva y,
desde el checkout fijado del analizador, llamar a la función pura:

```python
from pathlib import Path
from commerce_evaluation.exp01 import report

root = Path("copia-extraida")
result = report(
    root / "exp01-stack",
    root / "exp01-junit.xml",
    root / "exp01-outcomes.json",
    root / "exp01-manifest.json",
)
print(result["scenarios"], result["minimum_plan_counts"], result["validated"])
```

El inventario de los otros 30 escenarios y el próximo lote propuesto está en
`docs/design/exp-01-remaining.md`. Ninguno se ejecutó en esta unidad. P3 no queda desbloqueado.
