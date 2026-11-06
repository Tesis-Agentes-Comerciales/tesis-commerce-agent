# EXP-09 · CT19 HNP

CT19 HNP pasó en B2-HNP para un grant de una unidad de un único SKU observado. El comprador
sintético obtuvo una propuesta en `POST /delegations/proposals` y consintió su digest en
`POST /delegations`. El arnés verificó el SKU en `delegation_grants.scope_json`, retiró su
identidad del episodio desechable y luego disparó `POST /internal/hnp/tasks/{id}/run`.

El [protocolo](../../scenarios/ct19-hnp.json) se congeló en
`37bc44a9d15f02c2dd673586e82a763ae3203ea2`, antes de la corrida. Su SHA-256 es
`857eb123217d609d93c36a73e8ce452afb2b45b8bebd4f1e5e2be4db6b70db5c`.

## Observaciones

| Control | Merchant | Terminal del Agent | Efectos y atribución |
| --- | --- | --- | --- |
| Presente | Un `POST /checkout_sessions`, 201, mismo SKU y cantidad uno | `canceled / revoked_by_owner` | Una sesión preparada y cancelada, reserva `released / no_outbox`. Control positivo. |
| Retirada | Un `POST /checkout_sessions`, 404 nativo `invalid_item` | `failed / checkout_failed` con `merchant_refusal=invalid_item` y D1 `VERIFIED_NOT_CREATED` | Cero checkouts aceptados, reservas o sustituciones. Única celda atribuida, CT19 HNP. |
| Stock agotado | Un `POST /checkout_sessions`, 409 nativo `out_of_stock` | `failed / checkout_failed` con `merchant_refusal=out_of_stock` y D1 `VERIFIED_NOT_CREATED` | Identidad conservada, cero checkouts aceptados o reservas. Control de contraste. |

`HnpAgent` ejecutó el flujo real de delegación con identidad sintética del arnés. El selector
determinista leyó el feed completo congelado. El fixture comprobó cero referencias a la variante
antes del estímulo, la lectura scoped y el reexport del catálogo. La eliminación afectó solo al
episodio objetivo. La variante siguió presente en un episodio testigo y se verificó la
restauración de las filas, restricciones y disparadores. El POST se correlacionó con
`request_observations` mediante `Request-Id` y el hash de la clave de idempotencia.

El proxy bloqueó las rutas de completion de Merchant y las escrituras al PSP. El control
positivo revocó el grant tras preparar checkout y antes de continuar hacia pago. Los
observadores registraron cero llamadas de completion o PSP, cero aprobaciones, órdenes,
mandatos cerrados, intentos de completar y efectos en la base del PSP. PostgreSQL desechable
se eliminó y el proxy se cerró.

## Reproducción y evidencia

- [Actions 42122901771](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/42122901771).
  Perfil manual `exp09-ct19-hnp` de `nightly.yml`. JUnit y recorder registraron 3 recogidos
  y 3 aprobados, con cero fallidos, errores, omitidos o no ejecutados. La puerta registró
  `validated=true`, sin incidencias.
- Agent ejecutado `f9e60ce68cd706b177b4729772f60178c7be1b98`. El árbol de
  `src/commerce_agent` fue `3316c4ce2a6f88f9222eeafd0a66bbf64148c2ce`.
- Merchant `abf30df3b6a611284dedcc26c00accac6a853329` y PSP
  `e6ab67b5963434a9a3caae2012b70232a3cca65d`. El manifiesto confirmó los tres checkouts limpios.
- [Artefacto de Actions](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/42122901771/artifacts/13458273799)
  y [ZIP original](runs/42122901771.zip), con [SHA-256](SHA256SUMS) igual al de GitHub.
  Verificadas 37 entradas indexadas, incluidos JUnit, manifiesto, observaciones, reportes,
  snapshots y trazas. Los alias de pytest coinciden por contenido.
- [CI general del SHA medido](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/42122896576).

La medición termina en la preparación delegada. No cubre selección semántica, un grant con
varios SKU, un feed actualizado tras la desaparición ni despacho, pago o compra. La evidencia
HP y sus intentos históricos permanecen en el [informe CT19 HP](../ct19-hp/README.md).
