# CT19 HP — real variant identity disappearance

Approved scope: the CT19 user instruction; harness-only, no product changes, no LLM or payments.
Base Agent adbe120cecf6bffc3eaf22df5c9270760697a93a (main CI 41960294446, all four jobs passed).
Merchant abf30df3b6a611284dedcc26c00accac6a853329 and PSP e6ab67b5963434a9a3caae2012b70232a3cca65d.

## Phase gate

First execute only the reversible-fixture control in Actions. Do not implement/execute the HP
purchase test until its terminal evidence proves the stimulus and exact restoration.
Select the first available positive-price candidate by (observed unit amount, variant ID)
from the whole exported Merchant catalog; require a real available Offer. Freeze all catalog
rows including updated_at, feed, schema constraints, effects, clocks, pins and oracle before mutation.
The trusted MerchantControl subprocess, guarded to this disposable PostgreSQL cluster, may
retire exactly that run/offer row only if the actor owns the run and no checkout references it.
No FK/triggers are disabled. Restore with the original full row via INSERT, preserving updated_at.
Use a separate export directory; never replace the feed already given to the Agent.
Prove disappearance using SQL, authenticated scoped Offer read and full feed reexport.
Verify all other catalog rows and constraints/triggers are unchanged. Audit offer.read writes
remain evidence: exact catalog restoration does not mean rollback of the whole database.
Every table of Merchant and PSP is counted/hashed independently; no new checkout or payment.

## HP experiment (pending phase gate)

Use the actual B2-HP PurchaseService, D1 and transparent ACP proxy. This is a contract test
with explicit observed SKU input, not conversation or semantic selection. After durable D1
allow and before forwarding the sole create POST, retire the selected identity; native Merchant
must return exactly 404 invalid_item. Independent stock-zero control must return 409 out_of_stock.
No substitution, second create, accepted checkout, approval, order, token or payment is allowed.
Never infer commercial false from transport/technical failure; such a run is error/blocked.
Reinsert the exact original catalog row and verify cleanup even if assertions fail.
HNP remains not_executed; historical CT13/14/15 and all matrix/oracle bytes are unchanged.

## Evidence

Reuse LiveStack/PurchaseLane/FaultProxy/MerchantControl and EXP-01 recorder/reporter in nightly.
Separate manually dispatched scopes, exact pins, no model credentials; no parallel infrastructure.
Retain JUnit, manifest, frozen protocol/input, observed JSON, full catalog rows, SQL/table hashes,
Offer audit, ACP status/body without headers, service logs, shutdown gate, original ZIP and hashes.
Fixture control is not attributed as a P0 scenario. Freeze a separate HP protocol before phase 2.
