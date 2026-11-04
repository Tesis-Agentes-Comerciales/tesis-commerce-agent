# MAIN-03 — baseline reproducible del nightly

Alcance: corregir P2 #157 después del merge de P1, sin cambiar producto, pruebas comerciales,
oráculos ni resultados anteriores. Main inicial: `c75f4f9c6231c717941f8ad16ec27cdef158a2d8`;
su CI `41759525476` terminó con los cuatro jobs satisfactorios antes de editar P2.
P2 incorpora ese main mediante merge commit, conservando `ad86bdd` y sus ancestros.

## Decisión y alternativas

El nightly anterior mezclaba repositorios hermanos móviles con una fixture P2 que exige
SHAs exactos. Un schedule podía omitir live por credencial ausente y aparecer satisfactorio.
JUnit/evidencia tampoco eran obligatorios. No se corrige eliminando guardas de SHA.

Se conserva un único perfil de baseline histórico de servicios en el workflow existente:
Merchant `abf30df3b6a611284dedcc26c00accac6a853329` y PSP
`e6ab67b5963434a9a3caae2012b70232a3cca65d`. Ambos son defaults manuales y fallbacks cuando
schedule o un input vacío no suministran refs. El SHA Agent es el HEAD efectivo de cada run;
por ello una ejecución nueva no se presenta como el run histórico 41563374624 reproducido.
Sus versiones, locks y fuente limpia se registran antes de iniciar servicios.

Mantener HEAD móviles y excluir silenciosamente P2 confundiría compatibilidad con reproducción.
No se implementa un perfil de versiones nuevas: un ref distinto del pin bloquea antes de
servicios. Si se desea ese experimento, hará falta un perfil explícito con su plan/versiones y
denominador separados; nunca sustituir los pines de la reproducción histórica por HEAD.

## Scope full y compatibilidad

Se revisaron los 13 módulos de integración existentes. El plan de 95 nodeids procede del
JUnit ya retenido de CI 41556976012 y queda versionado en
`tests/support/nightly-required-cases.json`. No es un oráculo comercial.

| Módulo / familia | Casos | Dependencias del baseline |
|---|---:|---|
| 06dc creation recovery | 34 | Lookup ACP, sesiones/revocación, proxy de fallos |
| 07a initial dispatch | 8 | Creación/lookup, persistencia Agent |
| 07bb guarded D1 | 10 | Guarded create/lookup del Merchant |
| 07bp preselection | 4 | Lectura Offer/feed y run aislado |
| D2 authorization | 2 | Aprobación HP, AP2 y PSP sintético |
| B2 governed purchase | 3 | AP2, órdenes/webhooks y autorización |
| POL02 / POL11 | 2 / 1 | Guardas deterministas y backend ACP |
| Browser / journeys | 1 / 6 | Node 24, auth/UI/Chromium y routers guionados |
| HNP | 16 | Merchant HNP-M1 y PSP HNP-P1 |
| P0 bridge | 3 | Pines exactos, exportación y cleanup sin pago |
| Purchase | 5 | ACP/AP2, pago sintético, idempotencia y recibos |

Los pines contienen los marcadores HNP requeridos; preflight los comprueba para impedir que
el skip histórico de una dependencia ausente se interprete como validación. Los tests y sus
aserciones de gobernanza/inventario/conciliación no se cambian. La compatibilidad escrita
no equivale a una ejecución satisfactoria: los resultados live se publican por separado.
El antiguo nightly de main 41653872780 tenía 87 passed/5 failed de navegador; no se borra,
no se atribuye al cambio de refs y no se promete corregirlo mediante esta unidad.

## Aislamiento y autorización de full

El usuario autorizó expresamente las compras sintéticas **existentes** de full, después de
aclarar que varias pruebas aprueban/completan órdenes. No autoriza nuevas celdas UX03/UX05,
dinero real, transacciones externas o LLM pagado. P2 específico sigue sin aprobar/pagar.

Se inspeccionaron `live_stack.py`, `live_purchase.py`, `live_browser.py` y los pines hermanos:
servicios HTTP/listeners/DB publicados en loopback, PostgreSQL con nombre único y digest,
SQLite desechable, claves/credenciales generadas por fixture, entorno limpio y routers
guionados sin modelo configurado. Merchant apunta al PSP que inicia PurchaseLane;
el PSP fijado confirma contra su PaymentStore SQLite y sus dos tarjetas de prueba, sin
pasarela bancaria. Las URLs de imágenes/catálogo no son endpoints de pago ni cotización.
No se leen `.env` personales ni se usan instrumentos/credenciales reales.

## Puertas y evidencia

- La ausencia de credencial de lectura hace fallar cualquier invocación requerida, incluidos
  schedule y full manual. El valor solo lo reciben los dos checkouts; el gate usa un booleano.
- Preflight exige checkouts limpios y pines; valida dependencias antes de arrancar servicios.
- Se reutiliza el recorder P1 para resultados Python y JUnit. El gate compara todos los
  nodeids con el plan: una colección vacía, caso ausente/extra/duplicado, skip, xfail/xpass,
  error de setup o fallo real impide éxito. No cambia la selección de tests.
- Se exige evidencia JSONL, bootstrap/retirada de cada stack y los tres registros P2 con
  sus verificaciones de limpieza. La falta de evidencia no es passed.
- Los artefactos `nightly-full-evidence` y `nightly-p0-catalog-bridge-evidence` están separados.
  Incluyen JUnit, outcomes, manifiesto, JSONL/logs/capturas, reporte y SHA256SUMS.
  Los 33 escenarios / 37 celdas nuevas permanecen sin ejecución atribuida; full no los valida.

Validación: CI general del nuevo HEAD; despacho P2 con scope explícito; despacho full **sin
inputs**, para comprobar exactamente los defaults. Mantener todos los fallos y separar la
validez del workflow de la corrección de las pruebas de producto. No merge en esta unidad.
