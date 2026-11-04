# EXP-04 — MA22 y MA23 HNP

Ambos pasan en el contrato explícito de revocar y consentir un grant nuevo. Sin edición
in-place ni modificaciones del producto. B2-HNP por API con autenticación sintética del arnés,
no LLM ni validación del JWT público de auth. Base producto Agent 65183d5, Merchant
abf30df3b6a611284dedcc26c00accac6a853329 y PSP e6ab67b5963434a9a3caae2012b70232a3cca65d,
fuentes limpias verificadas en preflight. Protocolo exp04-mandate-replacement.json conserva
bytes/oráculos desde a64c58c. Entradas/catálogo completo/ofertas/requests congelados antes de Agent.

| Run | SHA ejecutado | Resultado |
| --- | --- | --- |
| [41898600527](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41898600527) | a64c58c42f3b006d568c72c72d722c0d8f54c638 | MA22: 1 failed; arnés exigía intent/D1 ok al grant revocado antes de activación. Conservado. |
| [41898998656](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41898998656) | 27407914ba7e9a529bc2150ebb6309cf6dcb70fc | MA22: 1 passed, cero failed/error/skipped, validated=true, 15 hashes. |
| [41899310659](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41899310659) | 27407914ba7e9a529bc2150ebb6309cf6dcb70fc | MA23: pytest 1 failed por Y agotada antes de Agent; escenario not_executed, validated=false. |
| [41899701762](https://github.com/Tesis-Agentes-Comerciales/tesis-commerce-agent/actions/runs/41899701762) | e4f52977feb73c483110a90c83401138e3e3d65c | MA23: 1 passed, cero failed/error/skipped, validated=true, 15 hashes. |

MA22: grant original disponible; revocación duradera; consentimiento nuevo de la misma
variante con máximo igual al precio observado Merchant menos un centavo. Es presupuesto
sintético controlado, no precio comercial inventado. Trigger antiguo 409 y nuevo escalado
budget_not_satisfiable, sin POST de checkout.

MA23: presupuesto constante 40,500,000 centavos COP; grant X/Y revocado y consentimiento nuevo
solo Y. X sigue disponible y asequible, distractor adversarial. La primera Y más cara estaba
agotada, por lo que ese intento no ejecutó Agent; se corrige selección del fixture filtrando
por disponibilidad real, sin cambiar Merchant ni oráculo. Y usada es Q110461772-512GB, identidad
observada del episodio, nunca ID ganador del Agent. El nuevo alcance excluye X y solo permite
Y sobre el máximo: escalado budget_not_satisfiable sin comprar ninguna.

Verificación independiente de scopes/epochs del ledger, consentimiento, digests y firmas de
mandatos abiertos con verify_database existente. Intent/D1 del grant anterior es exactamente
not_applicable (no activado); el nuevo es ok. Consentimiento/open_mandates son ok para ambos;
revocación anterior y escalado nuevo verifican ok. La corrección del arnés exige esos estados
exactos, conservando verificaciones de firmas/límites. El verificador informa merchant_refs/
owner_ref no verificados sin vault key: no se atribuye esa autoridad adicional.

Cero POST creación, checkout aceptado, outbox/reservas/recibos, mandatos cerrados, órdenes,
tokens o pagos nuevos. Merchant completo y PSP idénticos según huellas independientes.
Ninguna defensa proxy tuvo que bloquear un intento comercial; si lo hubiera, fallaría.
Grants revocados, procesos cerrados, proxy_closed y postgres_removed verificados. Para el
intento anterior al Agent no se inventa limpieza por caso: aparece null; shutdown sí consta.
ZIP originales, JUnit, manifest, entradas, logs sanitizados y hashes conservados en Git.
No matriz/oráculos P0, producto, proveedores, locks, evidencia previa o trabajo de Jair alterado.
PR #165 draft sobre #164, sin merges. Progreso por modo en progress-2026-11-04.json.
