# Contrato de política B2

> Contrato de la propuesta original P1–P5. Las fases, outcomes y obligaciones deben ajustarse
> a la arquitectura que se cierre esta semana, incluida la separación entre pedir información
> y obtener autoridad. Véanse la [revisión D1/D2](design-review.md) y el
> [plan vigente](design-closure-2026-10-11.md).

## Puerto de aplicación

La aplicación depende de un puerto pequeño y no de OPA directamente:

```python
class PolicyEvaluator(Protocol):
    async def evaluate(self, policy_input: PolicyInput) -> PolicyDecision: ...
```

El adaptador B2 consulta un OPA fijado por versión. Tests unitarios pueden usar un evaluador local
determinista, pero producción experimental nunca sustituye un fallo del motor por `allow`.

## Entrada canónica

```json
{
  "schema_version": "1.0",
  "decision_id": "dec_...",
  "trace_id": "trace_...",
  "run_id": "run_...",
  "phase": "selection",
  "now": "2026-10-06T15:00:00Z",
  "principal": {
    "subject_hash": "sha256:...",
    "owns_run": true,
    "auth_strength": "jwt"
  },
  "intent": {
    "action": "purchase",
    "currency": "COP",
    "budget_max_minor": 100000000,
    "quantity": 1,
    "required_attributes": {"color": "black"},
    "missing_required_fields": []
  },
  "selection": {
    "selected_variant_id": "PIXEL-8-128-BLK",
    "eligible_variant_ids": ["PIXEL-8-128-BLK"],
    "rejection_counts": {"budget": 2},
    "provenance": {"description": "untrusted_catalog"}
  },
  "checkout": null,
  "approval": null,
  "prior_decisions": []
}
```

Reglas del ensamblador:

- solo incluye campos definidos por esquema; desconocidos se rechazan;
- `principal`, reloj, `run_id`, `trace_id`, revisiones y hashes son generados o leídos por servidor;
- no incluye bearer tokens, direcciones completas, instrumentos de pago, mandates, JWT de checkout,
  texto libre de catálogo ni razonamiento del modelo;
- moneda y montos usan enteros en unidad menor;
- todo campo externo incluye procedencia y pasa validación antes de llegar al evaluador.

## Salida canónica

```json
{
  "schema_version": "1.0",
  "outcome": "allow",
  "reason_codes": ["SELECTION_WITHIN_ELIGIBLE_SET"],
  "obligations": [
    {"type": "record_evidence"},
    {"type": "recheck_before_completion"}
  ],
  "policy_bundle_version": "commerce-governance/v1",
  "policy_bundle_digest": "sha256:..."
}
```

`outcome` admite solamente:

- `allow`: la fase puede avanzar, cumpliendo todas las obligaciones;
- `deny`: la fase termina o vuelve a un estado seguro;
- `ask_for_authority`: faltan datos o consentimiento que solo el humano puede proporcionar.

Una salida con campos desconocidos, sin razón, con obligación no soportada o con un cuarto outcome
es inválida y se transforma en `deny/POLICY_INVALID_RESPONSE`.

## Políticas mínimas

| ID | Regla | Resultado |
|---|---|---|
| GOV-001 | Una compra ambigua o sin campos indispensables no avanza | `ask_for_authority(MISSING_AUTHORITY)` |
| GOV-002 | La variante elegida debe estar en el conjunto elegible calculado | `deny(SELECTION_NOT_ELIGIBLE)` |
| GOV-003 | Cero candidatos elegibles produce respuesta de negocio, no 5xx ni checkout | `deny(NO_ELIGIBLE_CANDIDATE)` |
| GOV-004 | Total, moneda, cantidad y variante del checkout deben respetar intención y selección | `deny(CHECKOUT_CONSTRAINT_MISMATCH)` |
| GOV-005 | Checkout/revisión/expiración deben estar vigentes en presentación y aprobación | `deny(STALE_OFFER)` + volver a presentar |
| GOV-006 | Principal debe ser propietario y fingerprint debe coincidir exactamente | `deny(APPROVAL_BINDING_MISMATCH)` |
| GOV-007 | Completion exige P2–P5 permitidos y aprobación/AP2 vigentes | `deny(MISSING_GOVERNANCE_EVIDENCE)` |
| GOV-008 | Datos no confiables no pueden alterar restricciones, autoridad ni aprobación | `deny(UNTRUSTED_DATA_FLOW)` |
| GOV-009 | Error, timeout, bundle desconocido o `undefined` falla cerrado | `deny(POLICY_UNAVAILABLE)` |
| GOV-010 | Evidencia obligatoria debe persistirse antes de un efecto | `deny(EVIDENCE_WRITE_FAILED)` |

## Obligaciones soportadas

- `request_fields`: preguntar únicamente los campos enumerados;
- `refresh_checkout`: releer o recrear la oferta;
- `invalidate_approval`: marcar la evidencia previa como no reutilizable;
- `present_terms`: mostrar la instantánea cerrada en Trusted Surface;
- `record_evidence`: persistir la decisión redacted antes de avanzar;
- `recheck_before_completion`: ejecutar P5;
- `require_human_approval`: bloquear hasta una acción explícita del propietario.

Las obligaciones son ejecutadas por código conocido. OPA no llama herramientas ni servicios.

## Explicaciones

Cada `reason_code` tiene una plantilla en español. La decisión de seguridad y su significado no se
generan con el LLM. El modelo puede ayudar a redactar contexto no normativo después de la decisión,
pero la UI siempre conserva el código y mensaje canónicos.

Ejemplos:

| Código | Mensaje al usuario |
|---|---|
| `NO_ELIGIBLE_CANDIDATE` | “No encontré una opción que cumpla todas tus condiciones. No inicié una compra.” |
| `MISSING_AUTHORITY` | “Antes de elegir necesito que confirmes: {required_fields}.” |
| `STALE_OFFER` | “La oferta cambió. Revisa y aprueba los términos actualizados; no usamos tu aprobación anterior.” |
| `POLICY_UNAVAILABLE` | “No pude verificar las reglas de compra. No se realizó ningún cobro; intenta de nuevo.” |

## Operación del evaluador

- Endpoint lógico: `POST /v1/data/commerce/governance/decision`.
- Bundle: `commerce-governance/v1`, fijado por digest y validado antes de ponerse ready.
- Timeout inicial: 100 ms local, configurable y medido; no hay retry dentro del camino de efecto.
- Readiness: la API no acepta compras si el bundle requerido no está cargado.
- Logs: payload redacted, `decision_id`, `trace_id`, versión, resultado y latencia; nunca secretos.
- Modos: `shadow` registra sin cambiar B0; `enforce` aplica la salida. El modo se registra por run.
- Cambio de política: pull request, tests Rego, aprobación humana, versión nueva y replay del corpus.
