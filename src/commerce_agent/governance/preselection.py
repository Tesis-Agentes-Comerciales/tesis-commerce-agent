"""Experimental new backend selection after complete observations. No D1 or dispatch.

The auxiliary store owns fresh episodes only. A prior buyer/model choice is not
an input or an approval: this flow always computes a new governed selection.
"""

from datetime import UTC, datetime
from itertools import pairwise
from time import monotonic, sleep
from typing import Literal, Self
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import model_validator

from commerce_agent.acp.authority_observation import (
    AuthorityObservationClient,
    CatalogBinding,
    ObservationBlocked,
)
from commerce_agent.acp.feed import ProductCandidate, ProductFeed
from commerce_agent.acp.offer_snapshot import (
    DeliveryContext,
    MerchantOffer,
    SnapshotModel,
    merchant_time,
    offer_snapshot_sha256,
)
from commerce_agent.domain import PurchaseIntent, SelectionResult, rank_candidates
from commerce_agent.governance.canonical import canonical_bytes, parse_model, sha256_digest
from commerce_agent.governance.remote_creation import CreationAbsent

# Merchant feed.py labels; unknown attributes follow the same capitalization rule.
_LABELS = {
    "color": "Color",
    "ram": "RAM",
    "gpu": "GPU",
    "chip": "Procesador",
    "cpu": "Procesador",
    "storage": "Almacenamiento",
    "screen": "Pantalla",
    "battery": "Batería",
    "os": "Sistema operativo",
    "size": "Tamaño",
    "resolution": "Resolución",
    "refresh_rate": "Refresco",
    "panel": "Panel",
    "switch": "Switch",
    "layout": "Distribución",
    "connectivity": "Conectividad",
}


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def after(previous: str) -> str:
    """Wait for an actual later clock sample; never increment/fabricate timestamps.

    Windows may return the same sample for adjacent calls. Clock rollback or a
    stopped clock blocks after one second; it never manufactures prior evidence.
    """
    deadline = monotonic() + 1
    while True:
        observed = now()
        if instant(observed) > instant(previous):
            return observed
        if monotonic() >= deadline:
            raise ObservationBlocked("OBSERVATION_CLOCK_UNAVAILABLE")
        sleep(0.001)


def instant(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not value.endswith("Z") or result.utcoffset() != UTC.utcoffset(result):
        raise ValueError("UTC observation clock required")
    return result


def projected_candidate(offer: MerchantOffer, origin: str) -> ProductCandidate:
    values: dict[str, str] = {}
    if offer.color:
        values["color"] = offer.color
    for key, value in offer.attributes.items():
        values.setdefault(key, ("sí" if value else "no") if isinstance(value, bool) else str(value))
    keys = [key for key in _LABELS if key in values] + sorted(set(values) - _LABELS.keys())
    url = origin + "/products/" + offer.product_id
    return ProductCandidate(
        product_id=offer.product_id,
        product_title=offer.product_title or f"{offer.brand} {offer.product_id}",
        variant_id=offer.id,
        variant_title=offer.name,
        product_url=url,
        variant_url=url + "?variant=" + offer.id,
        observed_unit_amount_minor=offer.pricing.items_total_minor,
        currency="COP",
        available=offer.available_quantity > 0,
        availability_status=(
            "out_of_stock"
            if offer.available_quantity == 0
            else "limited_stock"
            if offer.available_quantity == 1
            else "in_stock"
        ),
        categories=(offer.category,),
        brands=(offer.brand,),
        conditions=(offer.condition,),
        options=tuple(
            (_LABELS.get(key, key.replace("_", " ").capitalize()), values[key]) for key in keys
        ),
    )


class Episode(SnapshotModel):
    schema_version: Literal["commerce-preselection-episode/1"] = "commerce-preselection-episode/1"
    episode_id: str
    owner_id: str
    binding: CatalogBinding
    feed: ProductFeed
    intent: PurchaseIntent
    destination: DeliveryContext
    discovered_at: str
    selection_origin: Literal["new_backend_selection_not_human_approval"] = (
        "new_backend_selection_not_human_approval"
    )


class OfferObservation(SnapshotModel):
    schema_version: Literal["commerce-authority-observation/1"] = "commerce-authority-observation/1"
    observation_id: str
    episode_id: str
    episode_digest: str
    candidate: ProductCandidate
    offer: MerchantOffer
    offer_snapshot_profile: Literal["merchant-offer-snapshot/1"] = "merchant-offer-snapshot/1"
    offer_snapshot_sha256: str
    quantity: Literal[1] = 1
    observed_at: str
    request_id: str

    @model_validator(mode="after")
    def digest(self) -> Self:
        if offer_snapshot_sha256(self.offer) != self.offer_snapshot_sha256:
            raise ValueError("Offer snapshot substitution")
        return self


def episode_digest(episode: Episode) -> str:
    return sha256_digest(b"commerce-preselection-episode/v1\0" + canonical_bytes(episode))


def check_observation(episode: Episode, observation: OfferObservation, at: str) -> None:
    offer = observation.offer
    if (
        observation.episode_id != episode.episode_id
        or observation.episode_digest != episode_digest(episode)
        or episode.feed.candidate(offer.id) != observation.candidate
        or projected_candidate(offer, episode.binding.merchant_origin) != observation.candidate
        or offer.merchant_id != episode.binding.merchant_id
        or offer.delivery_context != episode.destination
        or episode.feed.metadata.target_country != episode.destination.country
    ):
        raise ObservationBlocked("CANDIDATE_OR_CONTEXT_CONFLICT")
    # Complete-terms complement, not a D1 policy verdict or a ranking modification.
    if offer.product_status != "active" or offer.available_quantity < 1:
        raise ObservationBlocked("OFFER_UNAVAILABLE")
    if not merchant_time(offer.observed_at) <= instant(at) < merchant_time(offer.expires_at):
        raise ObservationBlocked("OFFER_CLOCK_CONFLICT")


class AuthoritySelection(SnapshotModel):
    schema_version: Literal["commerce-authority-selection/1"] = "commerce-authority-selection/1"
    episode: Episode
    observations: tuple[OfferObservation, ...]
    scope_checks: tuple[CreationAbsent, CreationAbsent]
    checked_at: str
    selected_at: str
    sealed_at: str
    selection: SelectionResult
    terms_control: Literal["complete-terms-complement/1"] = "complete-terms-complement/1"
    human_approval: Literal[False] = False
    commercial_continuation: Literal[False] = False

    @model_validator(mode="after")
    def consistent(self) -> Self:
        for check in self.scope_checks:
            if (
                check.result != "not_found"
                or check.run_id != self.episode.binding.scope.remote_run_id
                or check.actor_id != self.episode.binding.scope.remote_actor_id
            ):
                raise ValueError("Authenticated scope conflict")
        candidates = self.episode.feed.available_candidates
        if (
            not candidates
            or len(candidates) > 512
            or tuple(o.candidate for o in self.observations) != candidates
            or len({o.observation_id for o in self.observations}) != len(candidates)
        ):
            raise ValueError("Incomplete observation set")
        times = [
            self.episode.discovered_at,
            *(o.observed_at for o in self.observations),
            self.checked_at,
            self.selected_at,
            self.sealed_at,
        ]
        parsed = [instant(t) for t in times]
        if any(first >= second for first, second in pairwise(parsed)):
            raise ValueError("Observation must precede the new selection and seal")
        for observation in self.observations:
            check_observation(self.episode, observation, self.sealed_at)
        expected = rank_candidates(self.episode.feed, self.episode.intent)
        if self.selection != expected or expected.selected is None:
            raise ValueError("New governed selection required")
        selected = next(o.offer for o in self.observations if o.candidate == expected.selected)
        hard = self.episode.intent.hard
        if (
            hard.max_total_amount_minor is not None
            and selected.pricing.total_minor > hard.max_total_amount_minor
        ):
            raise ObservationBlocked("COMPLETE_TOTAL_EXCEEDS_INTENT")
        return self


class PreselectionAuthority:
    """Explicit backend experiment. No Settings, API routes, LLM or checkout port."""

    def __init__(self, client: AuthorityObservationClient, store):
        self._client, self._store = client, store

    def begin(self, *, owner_id: str, intent: PurchaseIntent, destination: DeliveryContext) -> str:
        feed = self._client.discover()
        # Validate all origins; even URLs not used for requests must correspond.
        if any(
            urlsplit(c.product_url).netloc != urlsplit(self._client.binding.merchant_origin).netloc
            for c in feed.candidates
        ):
            raise ObservationBlocked("FEED_ORIGIN_CONFLICT")
        if not 0 < len(feed.available_candidates) <= 512:
            raise ObservationBlocked("DISCOVERY_BOUND_EXCEEDED")
        episode = parse_model(
            Episode,
            canonical_bytes(
                Episode(
                    episode_id=str(uuid4()),
                    owner_id=owner_id,
                    binding=self._client.binding,
                    feed=feed,
                    intent=intent,
                    destination=destination,
                    discovered_at=now(),
                )
            ),
        )
        self._store.open(episode)
        return episode.episode_id

    def capture(self, episode_id: str, *, owner_id: str) -> AuthoritySelection:
        # Durable single claim BEFORE reads; restart never grants a second capture.
        episode = self._store.claim(episode_id, owner_id, self._client.binding)
        try:
            first_scope = self._client.verify_scope()
            observations = []
            for candidate in episode.feed.available_candidates:
                offer, request_id = self._client.observe(candidate.variant_id, episode.destination)
                observation = OfferObservation(
                    observation_id=str(uuid4()),
                    episode_id=episode_id,
                    episode_digest=episode_digest(episode),
                    candidate=candidate,
                    offer=offer,
                    offer_snapshot_sha256=offer_snapshot_sha256(offer),
                    observed_at=after(
                        observations[-1].observed_at if observations else episode.discovered_at
                    ),
                    request_id=request_id,
                )
                self._store.append(observation)
                check_observation(episode, observation, observation.observed_at)
                observations.append(observation)
            last_scope = self._client.verify_scope()
            checked_at = after(observations[-1].observed_at)
            for observation in observations:
                check_observation(episode, observation, checked_at)
            selection = rank_candidates(episode.feed, episode.intent)
            # Ranking is provisional until the complete-terms complement succeeds.
            # The D1 engine is not called and its verdict is never represented here.
            if selection.selected is None:
                raise ObservationBlocked("NO_COMPATIBLE_SELECTION")
            selected_offer = next(
                o.offer for o in observations if o.candidate == selection.selected
            )
            limit = episode.intent.hard.max_total_amount_minor
            if limit is not None and selected_offer.pricing.total_minor > limit:
                raise ObservationBlocked("COMPLETE_TOTAL_EXCEEDS_INTENT")
            selected_at = after(checked_at)
            result = AuthoritySelection(
                episode=episode,
                observations=tuple(observations),
                scope_checks=(first_scope, last_scope),
                checked_at=checked_at,
                selected_at=selected_at,
                sealed_at=after(selected_at),
                selection=selection,
            )
            self._store.seal(result)
            return self._store.recover(episode_id, owner_id)
        except Exception:
            self._store.block(episode_id)
            raise ObservationBlocked("PRESELECTION_BLOCKED") from None
