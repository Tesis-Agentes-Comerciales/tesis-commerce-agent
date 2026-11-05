"""Delegation routes (docs/design/hnp-governed.md §5.1, §5.2), mounted only with AGENT_HNP_ENABLED.

Buyer routes take the buyer's JWT and see only the buyer's rows: another owner's proposal or
grant is not found. `/internal/hnp/*` take the service or the operator token, compared in
constant time, and refuse any request with an `Origin` header, so a browser cannot reach them.
Bodies are read as strict JSON here, so a bad body gets this API's `{code, message}`; the HP
routes keep FastAPI's own validation errors.

`POST /internal/hnp/tasks/{id}/run` claims the task in the request and resumes it in the
background: the answer is the task as the claim left it.
"""

import hmac
import logging
from collections.abc import Callable
from typing import Annotated, Protocol

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, SecretStr

from commerce_agent.buyer_auth import BuyerPrincipal
from commerce_agent.delegation.contract import (
    HNP_MESSAGES,
    DelegationList,
    DelegationProposal,
    DelegationView,
    RevokeAllReceipt,
    RevokeReceipt,
    TaskView,
)
from commerce_agent.delegation.models import Execution, KillSwitchState, Step
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
    "delegation_destination_unsupported": (
        422,
        "El comercio solo entrega en Bogotá, DC, Colombia, con código postal 110111.",
    ),
    "delegation_proposal_not_found": (404, "No encontré esa propuesta de delegación."),
    "delegation_scope_mismatch": (
        409,
        "Lo que autorizaste no coincide con la propuesta. Revísala de nuevo antes de autorizar.",
    ),
    "delegation_proposal_superseded": (
        409,
        "Esta propuesta fue reemplazada por una más reciente. Revisa la última.",
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
    "delegation_not_revocable": (409, HNP_MESSAGES["revoke.not_revocable"]),
    "hnp_unauthorized": (401, "Esta ruta exige la credencial del servicio o del operador."),
    "hnp_origin_forbidden": (403, "Esta ruta no acepta peticiones desde un navegador."),
    "hnp_task_not_found": (404, "No encontré esa tarea delegada."),
    "hnp_task_not_runnable": (409, "Esa tarea delegada ya terminó o se está ejecutando."),
    "hnp_request_invalid": (422, "La solicitud no es válida."),
}


_log = logging.getLogger(__name__)


class TaskRunner(Protocol):
    """`HnpExecutionService`: the claim, then the rest of the run from what it claimed."""

    def claim(self, task_id: str) -> Step: ...

    def resume(self, ex: Execution) -> Step: ...


def _resume(executor: TaskRunner, ex: Execution) -> None:
    """The run after the answer. Whatever escapes it leaves the task running for a later run."""
    try:
        executor.resume(ex)
    except Exception as error:
        _log.error("HNP task %s stopped: %s", ex.task.task_id, type(error).__name__)


class KillSwitchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    enabled: bool


async def _raw_body(request: Request) -> bytes:
    return await request.body()


RawBody = Annotated[bytes, Depends(_raw_body)]


def _parsed[Model: BaseModel](model: type[Model], body: bytes, code: str) -> Model:
    """Strict JSON: arrays read as tuples, duplicate keys and non-JSON numbers refused."""
    try:
        return parse_model(model, body)
    except ValueError:
        raise DelegationError(code) from None


def _error_response(_: Request, error: Exception) -> JSONResponse:
    code = error.code if isinstance(error, DelegationError) else "delegation_request_invalid"
    status, message = _ERRORS[code]
    return JSONResponse({"code": code, "message": message}, status_code=status)


def _internal(token: SecretStr) -> Callable[..., None]:
    expected = token.get_secret_value().encode()

    def guard(
        origin: Annotated[str | None, Header()] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        if origin is not None:
            raise DelegationError("hnp_origin_forbidden")
        scheme, _, supplied = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.encode(), expected):
            raise DelegationError("hnp_unauthorized")

    return guard


def register_delegation_api(
    app: FastAPI,
    service: DelegationService,
    *,
    executor: TaskRunner,
    current_buyer: Callable[..., BuyerPrincipal],
    service_token: SecretStr,
    operator_token: SecretStr,
) -> None:
    router = APIRouter()

    @router.post("/delegations/proposals", status_code=201)
    def propose(
        buyer: Annotated[BuyerPrincipal, Depends(current_buyer)], body: RawBody
    ) -> DelegationProposal:
        request = _parsed(DelegationProposalRequest, body, "delegation_request_invalid")
        return service.propose(buyer.id, request)

    @router.post("/delegations", status_code=201)
    def consent(
        buyer: Annotated[BuyerPrincipal, Depends(current_buyer)], body: RawBody, response: Response
    ) -> DelegationView:
        request = _parsed(DelegationConsentRequest, body, "delegation_request_invalid")
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

    @router.post("/delegations/{grant_id}/revoke")
    def revoke(
        grant_id: str, buyer: Annotated[BuyerPrincipal, Depends(current_buyer)]
    ) -> RevokeReceipt:
        return service.revoke(buyer.id, grant_id)

    @router.post("/me/delegations/revoke-all")
    def revoke_all(buyer: Annotated[BuyerPrincipal, Depends(current_buyer)]) -> RevokeAllReceipt:
        return service.revoke_all(buyer.id)

    @router.get("/internal/hnp/tasks/{task_id}", dependencies=[Depends(_internal(service_token))])
    def task(task_id: str) -> TaskView:
        return service.task(task_id)

    @router.post(
        "/internal/hnp/tasks/{task_id}/run",
        status_code=202,
        dependencies=[Depends(_internal(service_token))],
    )
    def run(task_id: str, response: Response, background: BackgroundTasks) -> TaskView:
        claimed = executor.claim(task_id)
        if claimed.status == "not_found":
            raise DelegationError("hnp_task_not_found")
        if claimed.status == "not_runnable":  # terminal, or a lease still held
            raise DelegationError("hnp_task_not_runnable")
        if claimed.status == "applied" and claimed.execution is not None:
            background.add_task(_resume, executor, claimed.execution)
        else:
            response.status_code = 200  # the claim itself ended the task
        return service.task(task_id)

    @router.post("/internal/hnp/kill-switch", dependencies=[Depends(_internal(operator_token))])
    def kill_switch(body: RawBody) -> KillSwitchState:
        request = _parsed(KillSwitchRequest, body, "hnp_request_invalid")
        return service.set_kill_switch(request.enabled)

    app.include_router(router)
    app.add_exception_handler(DelegationError, _error_response)
