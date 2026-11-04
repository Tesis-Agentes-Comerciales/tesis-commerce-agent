# P3-V01: Offer → Checkout contract validation

Status: implemented for review; real-service validation pending. P3 remains **BLOCKED**.
This is a technical contract validation, not any UX03 cell or conversational evaluation.
No changes to Agent product, Merchant, PSP, existing P0 matrix/oracles or P1/P2/P3 evidence.

## Fixed sources and execution

- Agent product: `a11f78bd5e5c18cd3a81ab74c2edc96e274367af`.
- Merchant: `abf30df3b6a611284dedcc26c00accac6a853329`.
- PSP: `e6ab67b5963434a9a3caae2012b70232a3cca65d`.

Dispatch existing `nightly.yml` on the PR branch with `test_scope=p3-offer-checkout`,
`merchant_ref` and `payment_ref` exactly as above. Sibling access, setup or pin errors fail.
Ubuntu 24.04, locked uv dependencies, existing LiveStack disposable PostgreSQL, PurchaseLane
ACP and FaultProxy are reused. No model, approval, delegation, completion or purchase is run.
Local collection skips live tests by design and is not real-service evidence.

## Preregistered technical controls

The immutable case list for this implementation is `tests/support/p3_contract.py:CASES`.
There are 24 controls: stable zero/positive shipping; budget T−1/T/T+1; revision-only;
shipping change invisible in feed; price change; stock exhausted/inactive; expiry −1/0/+1
second; timeout before forwarding; lost response after offer.read; malformed response;
schema-valid different offer; network error; foreign run/actor; missing auth; creation
idempotency/conflict; lost creation with lookup reconciliation; lost cancellation and replay.
Budget boundaries characterize comparison against real Merchant totals; this does not claim
conditional intent understanding or enforcement of a future UX03 policy by the product.

Each case gets two independently seeded complete catalogs with equal initial clock and all
Offer snapshots. Candidate attributes, stock and shipping select a deterministic technical
fixture; neither expected winners nor synthetic oracle prices are sent to Agent tools.
Calibration reads, creates, updates, gets and cancels a real checkout before validation.
Calibration total and snapshot are recorded before validation reads. The validation run must
be byte-equivalent in observed state after calibration cleanup. Deliberate adverse mutations
are limited to the validation clone and recorded separately: revision +1, price/shipping +1
minor unit, stock zero, inactive status or clock derived from the actual expiration.
Shipping feed equality is checked by re-exporting through the pinned Merchant, not by reading
an old export. Mutated disposable clones are quarantined, not restored backwards in revision.

## Assertions and evidence

Real ACPClient Offer reads and create/update/get/cancel are used. Public and persisted
quantity-one totals, currency, shipping, delivery context, Offer revision and expiry must
correspond. Update selects only IDs from that checkout. A separate test-only observer reports
`true`, `false` or `unknown`. A typed, attributable expiration can be false; transport, parsing,
identity/scope failure cannot. This observer is not new Agent behavior and never selects A/B.

`offer.read` payload/result/actor/request ID are checked against persisted run_events,
including the case where its successful response is lost. No-write timeout is injected before
forwarding. Ordinary ACP creation after an intervening mutation is characterized using current
Merchant terms: it does not prove atomic binding to an earlier read. Revision-only changes must
be detected despite equal totals. Schema-valid wrong-offer acceptance is a known client limit,
recorded explicitly rather than patched in product. Foreign credential scope is not inferred
from matching offer IDs.

The Merchant's separate, guarded control worker reads all public tables in repeatable-read,
publishing counts and hashes, public Offer/checkout snapshots and run_events only. No Agent
code imports Merchant or accesses its DB. All PSP SQLite tables are hashed/counted read-only;
no token/delegation/payment effect is permitted. Tables outside the explicit preparation,
audit and declared mutation effects must stay identical. Orders, completion attempts and
guarded attempts must remain zero. Every checkout is discovered independently in persistence,
cancelled and queried, including lost-response creations. Cancellation retains rows and audit;
cleanup means verified cancellation and no payment/order effect, not erasure of history.

The workflow always retains JUnit, sanitized JSONL/logs and a report with SHA-256 checksums.
The report lists passed, failed, error, skipped, blocked and not_executed; any missing case,
skip, setup failure or unverified cleanup prevents validation. Original failures must remain
accessible in their Actions runs; later fixes do not replace historical evidence. No private
DB, token, key or request authorization header is an artifact.

## Remaining decisions

A passing V01 demonstrates the bounded contract in a controlled synthetic clock/context only.
It cannot validate UX03/UX05, atomic read-to-create freshness, stock reservation, arbitrary
quantity/context/currency, real models or purchase authorization. Before UX03 implementation,
Yair must approve typed A/B intent and three-valued policy, identity/scope validation,
stale-read handling (refresh or existing guarded creation), frozen independent oracle custody
and calibration equivalence criteria. Any required product change needs separate approval.
