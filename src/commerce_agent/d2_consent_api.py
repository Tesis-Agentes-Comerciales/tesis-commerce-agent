"""Explicit experimental app factory; never mounted in the Agent runtime.

One backend-configured episode per instance. No cookies, CORS, model tools,
merchant gateway or execution routes. A deployment/IdP adapter is future work.
"""

import sqlite3
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import SecretStr

from commerce_agent.governance.dispatch_envelope import EnvelopeModel
from commerce_agent.governance.human_consent import ConsentBlocked, ConsentCommand
from commerce_agent.persistence.human_consent_sqlite import SqliteHumanConsent


class PresentationRequest(EnvelopeModel):
    """Intentionally empty: identity, episode and terms belong to the backend."""


def create_consent_app(store: SqliteHumanConsent) -> FastAPI:
    app = FastAPI(title="Experimental D2 terms consent", docs_url=None, redoc_url=None)
    bearer = HTTPBearer(auto_error=False)

    def credential(
        supplied: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> SecretStr:
        if supplied is None or supplied.scheme.lower() != "bearer":
            raise ConsentBlocked("HUMAN_AUTHENTICATION_REQUIRED")
        return SecretStr(supplied.credentials)

    def response(payload, status=200):
        return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})

    @app.exception_handler(ConsentBlocked)
    async def blocked(_request, error: ConsentBlocked):
        # Codes are library constants, never user input or exception text from a provider.
        code = str(error)
        status = 401 if "AUTHENTICATION" in code else 409
        return response({"error": code, "continuation_allowed": False}, status)

    @app.exception_handler(RequestValidationError)
    async def invalid(_request, _error):
        return response({"error": "INVALID_REQUEST", "continuation_allowed": False}, 422)

    @app.exception_handler(sqlite3.Error)
    async def persistence_failure(_request, _error):
        return response(
            {"error": "CONSENT_PERSISTENCE_UNAVAILABLE", "continuation_allowed": False}, 503
        )

    @app.post("/experimental/d2/presentations")
    def present(
        _body: PresentationRequest, authenticated: Annotated[SecretStr, Depends(credential)]
    ):
        issued = store.present(authenticated)
        # Intentional one-time delivery over a confidential backend channel.
        # SecretStr otherwise redacts this nonce in model dumps and diagnostics.
        return response(
            {
                "presentation": issued.presentation.model_dump(mode="json"),
                "challenge": issued.challenge.get_secret_value(),
                "delivery_assurance": "backend_issued_not_ui_seen",
            },
            201,
        )

    @app.post("/experimental/d2/decisions")
    def decide(body: ConsentCommand, authenticated: Annotated[SecretStr, Depends(credential)]):
        result = store.decide(authenticated, body)
        return response(
            {"receipt": result.model_dump(mode="json"), "continuation_allowed": False}, 201
        )

    @app.get("/experimental/d2/presentations/{presentation_id}")
    def state(presentation_id: str, authenticated: Annotated[SecretStr, Depends(credential)]):
        view, receipt = store.recover(authenticated, presentation_id)
        return response(
            {
                "presentation": view.model_dump(mode="json"),
                "state": receipt.decision if receipt else "pending",
                "receipt": receipt.model_dump(mode="json") if receipt else None,
                "continuation_allowed": False,
            }
        )

    return app
