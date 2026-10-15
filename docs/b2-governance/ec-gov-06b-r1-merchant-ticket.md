# Ticket separado — Replay estable de creación ACP

Estado: propuesto para revisión del responsable de `Maldinho18/tesis-commerce-merchant`.
No implementado en EC-GOV-06B-R1. Base reproducida: `70ce92b97cee3d7aabf8d287f0bf700816dc584b`.

## Problema y resultado esperado

Crear `POST /checkout_sessions` con K y cuerpo válido, incrementar la revisión
de la oferta y repetir el mismo POST devuelve 422 idempotency_conflict. Cambiar
solo el nombre devuelve el mismo ID pero distinto JSON. La respuesta original
perdida no puede recuperarse con una garantía independiente del catálogo actual.

Dentro del mismo run/actor autenticado, un POST idéntico con K ya consumida debe
resolver la creación original sin consultar oferta ni capacidades actuales.
K con contenido ACP distinto debe producir conflicto incluso si la nueva oferta
no existe. Una credencial de otro ámbito nunca recupera la creación original.

## Alcance mínimo a diseñar e implementar por separado

- `src/commerce_lab/acp.py`: validar/canonicalizar request del perfil y versión ACP;
  resolver operación existente antes de `_offer`; devolver respuesta original durable.
- `src/commerce_lab/checkout.py`: garantizar atomicidad entre creación y registro de
  identidad/respuesta ACP, incluyendo carreras; evitar ventana checkout confirmado
  sin información recuperable. Mantener contrato interno y no crear otra operación.
- Nueva migración aditiva: persistir fingerprint ACP (versión, perfil y cuerpo completo)
  y snapshot original de respuesta, ligados a run/actor/hash de K y checkout. No editar
  migraciones históricas. Definir estrategia fail-closed para registros anteriores sin
  esos datos; no reconstruir un supuesto original desde el catálogo actual.
- `src/commerce_lab/api.py` y `context.py`: conservar autenticación, códigos, cabeceras
  y esquema ACP; renovación solo para mismo run/actor. No exponer tokens ni K en logs.
- `tests/integration/test_acp_http.py`, `test_checkout.py`: regresiones PostgreSQL real.

No exponer un lookup por clave como consecuencia implícita de este ticket. Si se
elige esa alternativa, aprobar contrato separado: consulta autenticada y acotada al
mismo run/actor, fingerprint verificable, sin creación y sin interpretar ausencia
transitoria como no creación definitiva. Evaluar retención y revocación explícitamente.

## Criterios de aceptación

1. Commit de creación seguido de respuesta perdida y proceso cliente nuevo recupera
   ID y JSON originales; exactamente una fila/operación y ningún efecto de pago.
2. Repetir después de cambiar revisión, precio, stock, nombre o configuración de
   capacidades recupera original. Nunca omitir la validación de identidad del request.
3. K con otra variante/cuerpo/version soportada incompatible produce conflicto;
   inputs inválidos/versiones no soportadas siguen rechazados por su contrato.
4. Otro run, mismo actor y otro actor no filtran la creación. Expiración/revocación
   rechazan acceso; renovación al mismo ámbito permite recuperación autorizada.
5. Cuatro creaciones concurrentes y fallos entre escritura de checkout/registro ACP
   prueban atomicidad, único resultado y rollback consistente en PostgreSQL real.
6. Reinicio de proceso y de servicio, estados actuales modificados/cancelados y
   registros anteriores prueban distinción entre respuesta original y estado vigente.
7. No modificar selección B0, baseline, datos históricos, contratos congelados ACP/AP2
   ni servicios externos. Ejecutar regresión y comprobaciones oficiales Merchant.

La FK actual impide borrar físicamente una oferta con checkout; ensayar baja lógica
y no desactivar restricciones para producir un caso irreal. Si cambia el esquema,
añadir prueba de oferta ausente sin perder recuperabilidad del registro de creación.

Incidencia ambiental independiente observada: PostgreSQL con timezone America/Bogota
hace fallar `_as_timestamp`/parse_timestamp; con UTC la matriz pasa. Registrar
precondición UTC verificable o corregir conversión a UTC en otro cambio revisado.

Agent deberá integrar el contrato únicamente después de esta revisión: binding
remoto verificable, envelope versionado, recuperación sin duplicación y eventos
append-only/CAS para resolver uncertain. Este ticket no autoriza activar enforce.
