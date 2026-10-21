"""Delegation routes (docs/design/hnp-governed.md §5.1), mounted only with AGENT_HNP_ENABLED.

Buyer routes take the buyer's JWT and see only the buyer's rows: another owner's proposal or
grant is not found. Bodies are read as strict JSON here, so a bad body gets this API's
`{code, message}`; the HP routes keep FastAPI's own validation errors.
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from commerce_agent.buyer_auth import BuyerPrincipal
from commerce_agent.delegation.contract import DelegationList, DelegationProposal, DelegationView
from commerce_agent.delegation.service import (
    DelegationConsentRequest,
    DelegationError,
    DelegationProposalRequest,
    DelegationService,
)
from commerce_agent.governance.canonical import parse_model

_ERRORS: dict[str, tuple[int, str]] = {
    "delegation_request_invalid": (422, "La solicitud de delegación no es válida."),
    "delegation_variant_unavailable": (
        422,
        "Alguna de las opciones elegidas no está en el catálogo actual del comercio.",
    ),
    "delegation_validity_exceeded": (422, "La vigencia pedida supera la máxima permitida."),
    "delegation_proposal_not_found": (404, "No encontré esa propuesta de delegación."),
    "delegation_scope_mismatch": (
        409,
        "Lo que autorizaste no coincide con la propuesta. Revísala de nuevo antes de autorizar.",
    ),
    "delegation_proposal_expired": (
        409,
        "La propuesta venció antes de que la autorizaras. Crea una nueva.",
    ),
    "delegation_signer_unavailable": (
        503,
        "No pude firmar la autorización en este momento. Intenta de nuevo más tarde.",
    ),
    "delegation_not_found": (404, "No encontré esa delegación."),
}


async def _raw_body(request: Request) -> bytes:
    return await request.body()


RawBody = Annotated[bytes, Depends(_raw_body)]


def _parsed[Model: BaseModel](model: type[Model], body: bytes) -> Model:
    """Strict JSON: arrays read as tuples, duplicate keys and non-JSON numbers refused."""
    try:
        return parse_model(model, body)
    except ValueError:
        raise DelegationError("delegation_request_invalid") from None


def _error_response(_: Request, error: Exception) -> JSONResponse:
    code = error.code if isinstance(error, DelegationError) else "delegation_request_invalid"
    status, message = _ERRORS[code]
    return JSONResponse({"code": code, "message": message}, status_code=status)


def register_delegation_api(
    app: FastAPI,
    service: DelegationService,
    *,
    current_buyer: Callable[..., BuyerPrincipal],
) -> None:
    router = APIRouter()

    @router.post("/delegations/proposals", status_code=201)
    def propose(
        buyer: Annotated[BuyerPrincipal, Depends(current_buyer)], body: RawBody
    ) -> DelegationProposal:
        request = _parsed(DelegationProposalRequest, body)
        return service.propose(buyer.id, request)

    @router.post("/delegations", status_code=201)
    def consent(
        buyer: Annotated[BuyerPrincipal, Depends(current_buyer)], body: RawBody, response: Response
    ) -> DelegationView:
        request = _parsed(DelegationConsentRequest, body)
        view, created = service.consent(buyer.id, request)
        if not created:
            response.status_code = 200
        return view

    @router.get("/me/delegations")
    def my_delegations(buyer: Annotated[BuyerPrincipal, Depends(current_buyer)]) -> DelegationList:
        return service.delegations(buyer.id)

    @router.get("/delegations/{grant_id}")
    def delegation(
        grant_id: str, buyer: Annotated[BuyerPrincipal, Depends(current_buyer)]
    ) -> DelegationView:
        return service.delegation(buyer.id, grant_id)

    app.include_router(router)
    app.add_exception_handler(DelegationError, _error_response)
