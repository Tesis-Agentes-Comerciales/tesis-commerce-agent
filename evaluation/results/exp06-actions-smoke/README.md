# EXP-06 — orquestación manual en GitHub Actions

Estado: preparación sin consumo autorizado. El usuario eligió reutilizar Actions;
no se instalaron herramientas ni se cambiaron ajustes de Ubuntu/Docker locales.
Esta decisión sustituye el pendiente de host de la etapa anterior, cuya evidencia
permanece íntegra en `evaluation/results/exp06-protected-smoke/`.

## Frontera y pruebas

`exp06-smoke.yml` no tiene push, PR ni schedule. Su modo por defecto es validate,
presupuesto 0, autorización vacía y confirmaciones false. workflow_call sólo admite
agent_sha y MERCHANT_REPO_TOKEN: no puede transmitir modo pagado ni clave de modelo.
El puente nightly exige dispatch manual y fija ese modo de validación. Los perfiles
existentes y el cron no cambian; ninguno puede lanzar smoke pagado.

El modo smoke exige un dispatch directo del archivo exacto, repositorio correcto,
SHA completo igual al ref elegido, snapshot exacto, máximo USD >0 y <=9,
AUTHORIZE_EXP06_SMOKE y confirmaciones de proyecto/tarifas. El runtime coteja el
recibo con el evento original. run_attempt distinto de 1 se rechaza para impedir
que un rerun reinicie el presupuesto en otro runner. No se ha lanzado ese modo.

La guarda existente protege router, chooser, reviewer y retries mediante factory,
constructor SDK, frontera HTTP y transporte con reserva persistida. El adaptador
fija service_tier=default (también verificado en el wire) y rechaza otras tarifas;
una respuesta con tier inesperado conserva reserva y corta. Esto elimina la
dependencia del precio respecto a un default administrativo desconocido. Se
registra como configuración técnica adicional; no modifica Agent, prompts,
oráculos, instrucciones ni max_output_tokens.

Se conservan proyecto/hash originales, tres episodios HP y una repetición por
caso. El mismo orquestador congela los tres catálogos antes de la primera llamada,
usa autenticación real, observa efectos independientemente y elimina bases privadas.
Ninguna respuesta estructural se califica semánticamente a sí misma.

## Resultado offline desfavorable conservado

`offline-encoding-failure.xml`: 72 passed, 1 failed local. Un test leía YAML UTF-8
con la codificación predeterminada Windows cp1252. Se corrigió sólo la lectura
explícita UTF-8; no se cambió ninguna aserción, rúbrica o resultado esperado.
El JUnit posterior se conserva por separado. Los runs reales del workflow de
validación y el CI final se enlazan en el PR #170 con sus SHAs y artefactos.

## Cómo lanzar después de revisión y autorización

1. Incorporar el workflow a main mediante revisión/merge humano. GitHub necesita
   el archivo en la rama predeterminada para mostrar Run workflow; esta unidad no
   realiza merges. Antes de ello se comprueba con el puente reusable de validación.
2. Configurar o confirmar disponibilidad de EXP06_OPENAI_API_KEY en secretos Actions.
   La consulta del repositorio no encontró nombres EXP06_. No se copió ninguna clave
   local. EXP06_OPENAI_PROJECT_ID/EXP06_OPENAI_ORG_ID son opcionales según la cuenta;
   el proyecto de facturación debe ser el confirmado. El token de hermanos ya existe.
3. Actions > EXP-06 protected manual smoke > Run workflow. Primero validate: no
   recibe secretos de proveedor, ejecuta controles offline y el orquestador con
   ModelServer. Un éxito no ejecuta ni valida semánticamente UX01/UX11/UX12.
4. Sólo después de autorización explícita de modelo, presupuesto y ejecución:
   elegir smoke, SHA completo revisado igual al ref seleccionado,
   gpt-5-mini-2025-08-07, máximo USD autorizado y AUTHORIZE_EXP06_SMOKE; confirmar
   proyecto y tarifas. No usar rerun para continuar: requiere nueva decisión y dispatch.

El secreto se inyecta sólo en el paso de ejecución, después del gate. El receipt
queda fuera de los artefactos. Se archivan sólo evidencia pública y trazas sanitizadas,
sin JWT, claves, bases privadas ni valores de secretos. El gate técnico también
registra bloqueo/error si no llega a existir un reporte completo. CI verde significa
ejecución técnica completa; la semántica queda pending_review para dos humanos.

## Presupuesto y cuenta

Máximo teórico conservador estándar: USD 6,408 (18 intentos); reservas con margen:
USD 8,010; tope propuesto no autorizado: USD 9. Si no cabe USD 0,445 antes de un
intento, se corta sin enviarlo. Error/timeout/usage ausente conserva reserva.
Se registran caso, hashes, tier, intentos, retries, tokens y coste conocido frente
a reserva desconocida. La conciliación con factura/proyecto es posterior, con
ventana de ejecución y sin otro tráfico o declarando que no es atribuible.

La visibilidad del snapshot con la credencial local se comprobó por GET (200),
sin inferencia; no acredita acceso de un secreto GitHub todavía no configurado,
cuota, saldo ni factura. El paso pagado vuelve a comprobar visibilidad antes de
inferir. Snapshot deprecated, retirada anunciada 2026-12-11; sin sustitución.
No se ha autorizado ni ejecutado smoke pagado, lote de 15, HNP semántico ni CT19.
