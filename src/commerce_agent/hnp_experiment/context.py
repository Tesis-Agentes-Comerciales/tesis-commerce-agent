"""Backend-owned, read-only synthetic facts; no operational stores."""

from collections.abc import Callable
from threading import RLock

from pydantic import model_validator

from commerce_agent.acp.feed import ProductFeed
from commerce_agent.domain import PurchaseIntent, rank_candidates
from commerce_agent.governance.canonical import parse_model
from commerce_agent.governance.models import Id, Ids, UtcTime, Version

from .models import (
    SCHEMA,
    BuyerContext,
    Candidate,
    CheckId,
    ExperimentalModel,
    SourceRef,
    SyntheticDelegation,
    SyntheticTerms,
    TrustedReviewContext,
    digest,
    wire_bytes,
)


class SyntheticSource(ExperimentalModel):
    episode_id: Id
    version: Version
    intent: PurchaseIntent
    feed: ProductFeed
    terms: SyntheticTerms
    delegation: SyntheticDelegation
    required_constraint_ids: Ids
    authority_now: UtcTime
    authorized_merchant_ref: Id

    @model_validator(mode="after")
    def references(self):
        content = self.terms.model_dump(mode="json", exclude={"offer_ref"})
        if self.terms.offer_ref.digest != digest("hnp-source-offer/v1", content):
            raise ValueError("Unverifiable synthetic offer")
        return self


def source_ref(kind: str, version: int, value: object) -> SourceRef:
    return SourceRef(id=kind, version=version, digest=digest(f"hnp-source-{kind}/v1", value))


def assemble(source: SyntheticSource) -> TrustedReviewContext:
    source = parse_model(SyntheticSource, wire_bytes(source))
    ranked = rank_candidates(source.feed, source.intent)
    candidates = tuple(
        Candidate(
            product_id=c.product_id,
            variant_id=c.variant_id,
            conditions=c.conditions,
            observed_unit_minor=c.observed_unit_amount_minor,
            currency=c.currency.upper(),
        )
        for c in source.feed.candidates
    )
    body = dict(
        schema_version=SCHEMA,
        episode_ref=source_ref(source.episode_id, source.version, source.episode_id),
        intent_ref=source_ref("intent", source.version, source.intent),
        feed_ref=source_ref("feed", source.version, source.feed),
        delegation_ref=source_ref("delegation", source.version, source.delegation),
        intent=source.intent,
        required_constraint_ids=source.required_constraint_ids,
        candidates=candidates,
        eligible_variant_ids=tuple(sorted(c.candidate.variant_id for c in ranked.ranked)),
        authorized_merchant_ref=source.authorized_merchant_ref,
        terms=source.terms,
        delegation=source.delegation,
        authority_now=source.authority_now,
        protocol_verification="not_executed",
    )
    # Convert defaults and nested objects through their explicit serialized forms.
    import json

    from .models import wire_bytes as encode

    normalized = json.loads(encode(body))
    normalized["context_ref"] = SourceRef(
        id="context:" + source.episode_id,
        version=source.version,
        digest=digest("hnp-context/v1", normalized),
    ).model_dump(mode="json")
    return parse_model(TrustedReviewContext, wire_bytes(normalized))


def buyer_context(context: TrustedReviewContext) -> BuyerContext:
    return BuyerContext(
        intent=context.intent.model_copy(deep=True),
        intent_ref=context.intent_ref,
        candidates=tuple(c.model_copy(deep=True) for c in context.candidates),
        terms=context.terms.model_copy(deep=True),
        constraint_ids=context.required_constraint_ids,
    )


def check_refs(check: CheckId, context: TrustedReviewContext) -> tuple[SourceRef, ...]:
    if check == "selection":
        return context.intent_ref, context.feed_ref
    if check == "budget":
        return context.intent_ref, context.terms.offer_ref, context.delegation_ref
    if check == "offer":
        return (context.terms.offer_ref,)
    if check == "constraints":
        return (context.intent_ref,)
    if check == "binding":
        return (context.context_ref,)
    return (context.delegation_ref,)


class FixtureContextResolver:
    """A lock demonstrates version admission only; this is not SQLite D23 or AP2."""

    def __init__(self, source: SyntheticSource, next_source: SyntheticSource | None = None):
        self._context = assemble(source)
        self._lock = RLock()
        self._next_source = next_source

    def resolve(self) -> TrustedReviewContext:
        with self._lock:
            return parse_model(TrustedReviewContext, wire_bytes(self._context))

    def replace(self, source: SyntheticSource) -> None:
        updated = assemble(source)
        with self._lock:
            if updated.context_ref.version <= self._context.context_ref.version:
                raise ValueError("Context versions must advance")
            self._context = updated

    def commit_if_current(self, expected: SourceRef, action: Callable[[], None]) -> bool:
        with self._lock:
            if self._next_source is not None:
                self.replace(self._next_source)
                self._next_source = None
            if self._context.context_ref != expected:
                return False
            action()
            return True
