# Reportes EC-GOV (históricos)

Reportes de ejecución de los tickets de gobernanza B2, versionados entre el 14 y el 19 de octubre de 2026
mientras se implementaban D1, D17 y D2-HP. Son registros fechados: describen ramas, commits y
corridas de ese momento, no el estado actual del código, y no se reescriben.

Varias corridas se hicieron en Windows y conservan rutas como `C:\ANDES\Tesis\...`, comandos
PowerShell y `.venv/Scripts/python.exe`. Esas rutas son de la máquina donde se ejecutaron: no son
comandos para reproducir hoy. Las suites live se reemplazaron por el stack portable de
[tests/integration](../../../tests/integration/README.md) (macOS y Linux), y el estado vigente está en
[docs/b2-governance](../../b2-governance/README.md) y en el
[handoff de integración HP](../../b2-governance/hp-integration-handoff-2026-10-17.md).

La evidencia sellada HP/B2 v2 no se movió: sigue en
[docs/b2-governance/hp-b2-backend-validation-2026-10-17-v2](../../b2-governance/hp-b2-backend-validation-2026-10-17-v2/README.md)
con sus `hashes.sha256`.

| Reporte | Tema |
|---|---|
| [EC-GOV-04](ec-gov-04-evidence-results.md) | Ledger D1 de evidencia |
| [EC-GOV-05](ec-gov-05-shadow-readiness.md) | Contraste D1 con el runtime real |
| [EC-GOV-05A](ec-gov-05a-purchase-revision-results.md) | Revisión persistente de purchases y CAS |
| [EC-GOV-05B](ec-gov-05b-d1-provenance-results.md) | Procedencia operativa D1 |
| [EC-GOV-05C](ec-gov-05c-d1-shadow-results.md) | Integración D1 shadow |
| [EC-GOV-06A](ec-gov-06a-d1-uow-results.md) | Unidad de trabajo D1 local |
| [EC-GOV-06B](ec-gov-06b-d1-enforce-results.md) | Integración local D1 y frontera de despacho; sección M con la adenda de latencias del 19 de octubre |
| [EC-GOV-06B-R1](ec-gov-06b-r1-checkout-recovery.md) | Recuperación de creación ACP |
| [Ticket 06B-R1 Merchant](ec-gov-06b-r1-merchant-ticket.md) | Replay estable de creación ACP, pedido al Merchant |
| [EC-GOV-06C-A](ec-gov-06c-a-envelope-binding-results.md) | Envelope durable y binding esperado |
| [EC-GOV-06C-B](ec-gov-06c-b-reconciliation-results.md) | Núcleo transaccional de reconciliación segura |
| [EC-GOV-06C-C](ec-gov-06c-c-post-create-safety-results.md) | Seguridad posterior a la creación |
| [EC-GOV-06D-B](ec-gov-06d-b-remote-creation-verification-results.md) | Verificación remota de creación histórica ACP |
| [EC-GOV-06D-C](ec-gov-06d-c-live-recovery-integration-results.md) | Integración real de recuperación Agent–Merchant |
| [EC-GOV-07A](ec-gov-07a-hp-initial-dispatch-results.md) | Despacho inicial HP seguro |
| [EC-GOV-07B-P](ec-gov-07b-p-preselection-authority-results.md) | Observación autoritativa anterior a una nueva selección |
| [EC-GOV-07B-Q](ec-gov-07b-q-preselection-d1-binding-results.md) | Binding de preselección a D1 sin despacho |
| [EC-GOV-07B-B](ec-gov-07b-b-d17-integration-results.md) | Integración D1/07A/D17 |
| [EC-GOV-07C](ec-gov-07c-d2-hp-consent-results.md) | Consentimiento humano D2-HP vinculado a D17 |
