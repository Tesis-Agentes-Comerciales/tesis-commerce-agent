"""`POST /assistant`: the assistant-transport stream consumed by the assistant-ui runtime.

The backend owns the state. Each response is a stream of `update-state` operations that rebuild
`{"messages": [...], "purchase": ...}` on the client; see docs/agent-ui-contract.md.
"""

import asyncio
import copy
import json
import logging
import re
import threading
from collections.abc import Callable
from typing import Annotated, Any, Literal
from uuid import uuid4

from assistant_stream import RunController, create_run
from assistant_stream.serialization import AssistantTransportResponse
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from commerce_agent.acp.feed import ProductFeedError
from commerce_agent.application import CheckoutDetails
from commerce_agent.application.conversation import (
    PENDING_PURCHASE_MESSAGE,
    ConversationService,
    StepName,
    request_sha256,
)
from commerce_agent.buyer_auth import BuyerPrincipal
from commerce_agent.llm import ConversationMessage

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 1024 * 1024
MAX_MESSAGE_CHARS = 2_000
MAX_HISTORY_MESSAGES = 20
MAX_DISPLAY_MESSAGES = 200
MAX_COMMANDS = 8
RUN_ID_PATTERN = r"^run_[A-Za-z0-9_-]{1,128}$"

GUARD_TEXT = PENDING_PURCHASE_MESSAGE
PRICE_CHANGED_NOTE = "El precio cambió frente al feed; usé el total actual del checkout."
DEFAULT_ERROR = "Ocurrió un error inesperado."
CANCELLED_STEP = "Se canceló la solicitud."
ERROR_MESSAGES = {
    # Same copy the UI used for these codes before the transport runtime.
    "no_candidate": "No encontré un producto que cumpla esas condiciones.",
    "budget_exceeded": "El total autoritativo del comercio supera tu presupuesto.",
    "intent_model_error": "No pude interpretar la solicitud con el modelo en este momento.",
    "transport_error": "No pude comunicarme con el ecommerce ACP.",
    "timeout": "El ecommerce tardó demasiado en responder.",
    "INVALID_INPUT": "El ecommerce rechazó los datos del checkout. Revisa tus datos de entrega.",
    "out_of_stock": "El producto se agotó en el comercio. Elige otro.",
    "invalid_item": "El producto ya no está disponible en el comercio.",
    "PROVIDER_UNAVAILABLE": "El proveedor de pagos del comercio no está disponible.",
    "http_error": "El ecommerce no pudo procesar la solicitud.",
    "catalog_unavailable": "El catálogo del comercio no está disponible en este momento.",
    "turn_conflict": "Este envío ya se procesó con otro contenido. Escribe el mensaje de nuevo.",
    "purchase_not_found": "No encontré esa compra.",
}
PURCHASE_UPDATE_TEXT = {
    "awaiting_confirmation": "La compra sigue esperando tu aprobación.",
    "approved": "Registré tu aprobación. La compra está lista para completarse.",
    "completing": "La compra se está completando con el comercio.",
    "uncertain": (
        "No pude confirmar el resultado con el comercio. Revisa el estado de la compra antes de "
        "intentarlo de nuevo."
    ),
    "canceled": "Cancelé la compra pendiente. No se realizó ningún cobro.",
    "failed": "La compra no se pudo completar.",
}

IdFactory = Callable[[str], str]
Operation = dict[str, Any]
Emit = Callable[[list[Operation]], None]


def default_id_factory(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def error_message(code: str | None) -> str:
    return ERROR_MESSAGES.get(code or "", DEFAULT_ERROR)


class TurnCancelled(Exception):
    """Raised inside the worker when the client disconnected; never shown to the client."""


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TextPart(RequestModel):
    type: Literal["text"]
    text: str = Field(max_length=MAX_MESSAGE_CHARS)


class UserMessage(RequestModel):
    role: Literal["user"]
    parts: list[TextPart] = Field(min_length=1, max_length=8)

    @property
    def text(self) -> str:
        return "\n".join(part.text for part in self.parts).strip()

    @model_validator(mode="after")
    def validate_text(self) -> "UserMessage":
        if not self.text:
            raise ValueError("Message text is empty")
        if len(self.text) > MAX_MESSAGE_CHARS:
            raise ValueError("Message text is too long")
        return self


class AddMessageCommand(RequestModel):
    type: Literal["add-message"]
    message: UserMessage
    parentId: str | None = Field(default=None, max_length=128)
    sourceId: str | None = Field(default=None, max_length=128)


class PurchaseUpdatedCommand(RequestModel):
    type: Literal["purchase-updated"]
    runId: str = Field(pattern=RUN_ID_PATTERN)


class AddToolResultCommand(BaseModel):
    """Accepted for runtime compatibility and ignored in this version."""

    model_config = ConfigDict(extra="allow", strict=True)

    type: Literal["add-tool-result"]


Command = Annotated[
    AddMessageCommand | PurchaseUpdatedCommand | AddToolResultCommand,
    Field(discriminator="type"),
]


class AssistantRequest(BaseModel):
    # The runtime spreads callSettings and config at the top level; unknown keys are ignored and
    # system, tools, callSettings, config, threadId and parentId are never read.
    model_config = ConfigDict(extra="ignore", strict=True)

    commands: list[Command] = Field(min_length=1, max_length=MAX_COMMANDS)
    turn_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    conversation_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,128}$")
    details: CheckoutDetails | None = None
    state: Any = None

    @model_validator(mode="after")
    def validate_details(self) -> "AssistantRequest":
        if self.details is None and any(
            isinstance(command, AddMessageCommand) for command in self.commands
        ):
            raise ValueError("add-message requires checkout details")
        return self


def _text_of(parts: list[Any]) -> str:
    texts = [
        part["text"]
        for part in parts
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part["text"], str)
    ]
    return "\n".join(texts).strip()


def _display_part(part: object) -> dict[str, Any] | None:
    if not isinstance(part, dict):
        return None
    kind = part.get("type")
    if kind in {"text", "reasoning"}:
        text = part.get("text")
        return {"type": kind, "text": text} if isinstance(text, str) else None
    if kind == "tool-call":
        call_id = part.get("toolCallId")
        name = part.get("toolName")
        if not isinstance(call_id, str) or not isinstance(name, str):
            return None
        kept = {"type": "tool-call", "toolCallId": call_id, "toolName": name}
        for key in ("args", "argsText", "result", "isError"):
            if key in part:
                kept[key] = part[key]
        return kept
    return None


def sanitize_state(raw: object, new_id: IdFactory) -> tuple[list[dict[str, Any]], str | None]:
    """Keep displayable messages from untrusted client state and the run ID it claims.

    Nothing here is authoritative: messages are echoed back only for display, their text parts
    become model history, and the claimed run ID is verified against SQLite before use.
    """
    if not isinstance(raw, dict):
        return [], None
    messages: list[dict[str, Any]] = []
    raw_messages = raw.get("messages")
    for item in raw_messages[-MAX_DISPLAY_MESSAGES:] if isinstance(raw_messages, list) else []:
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        parts = [part for part in map(_display_part, content) if part is not None]
        identifier = item.get("id")
        message: dict[str, Any] = {
            "id": identifier
            if isinstance(identifier, str) and 0 < len(identifier) <= 128
            else new_id("msg"),
            "role": item["role"],
            "content": parts,
        }
        if item["role"] == "assistant":
            message["status"] = _settled_status(item.get("status"))
        messages.append(message)
    run_id: str | None = None
    purchase = raw.get("purchase")
    if isinstance(purchase, dict):
        candidate = purchase.get("run_id")
        if isinstance(candidate, str) and len(candidate) <= 132 and candidate.startswith("run_"):
            run_id = candidate
    return messages, run_id


def _approval_run_id(part: dict[str, Any]) -> str | None:
    result = part.get("result")
    run_id = result.get("run_id") if isinstance(result, dict) else None
    if isinstance(run_id, str) and re.fullmatch(RUN_ID_PATTERN, run_id):
        return run_id
    return None


def _settled_status(status: object) -> dict[str, Any]:
    if isinstance(status, dict):
        kind = status.get("type")
        if kind == "complete":
            return {"type": "complete", "reason": "stop"}
        if kind == "incomplete" and status.get("reason") == "error":
            error = status.get("error")
            return {
                "type": "incomplete",
                "reason": "error",
                "error": error if isinstance(error, str) else DEFAULT_ERROR,
            }
    # A message left running by an interrupted stream is no longer running.
    return {"type": "incomplete", "reason": "cancelled"}


def model_history(messages: list[dict[str, Any]], latest: str) -> tuple[ConversationMessage, ...]:
    history: list[ConversationMessage] = []
    for message in messages:
        text = _text_of(message["content"])[:MAX_MESSAGE_CHARS].strip()
        if text:
            history.append(ConversationMessage(role=message["role"], content=text))
    history.append(ConversationMessage(role="user", content=latest))
    return tuple(history[-MAX_HISTORY_MESSAGES:])


def purchase_update_text(view: dict[str, Any]) -> str:
    stage = view["stage"]
    if stage == "completed":
        order_id = view.get("order_id")
        if isinstance(order_id, str) and order_id:
            return f"La compra quedó completada. El comercio registró la orden {order_id}."
        return "La compra quedó completada."
    return PURCHASE_UPDATE_TEXT.get(stage, "Actualicé el estado de la compra.")


class AssistantMessageRecorder:
    """Builds one assistant message and emits the matching state operations.

    It keeps its own plain copy of the message, so the worker thread can cache the final message
    while the event loop applies the same operations to `controller.state`.
    """

    def __init__(
        self,
        *,
        index: int,
        new_id: IdFactory,
        emit: Emit,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> None:
        self._index = index
        self._new_id = new_id
        self._emit = emit
        self._cancelled = cancelled
        self._open_step: int | None = None
        self.message: dict[str, Any] | None = None

    @property
    def _path(self) -> list[str | int]:
        return ["messages", self._index]

    def _content(self) -> list[dict[str, Any]]:
        if self.message is None:
            self.message = {
                "id": self._new_id("msg"),
                "role": "assistant",
                "status": {"type": "running"},
                "content": [],
            }
            self._send([{"type": "set", "path": self._path, "value": self.message}])
        return self.message["content"]

    def _send(self, operations: list[Operation]) -> None:
        # Values are copied so later local edits never leak into queued operations.
        self._emit(copy.deepcopy(operations))

    def _check_cancelled(self) -> None:
        if self._cancelled():
            raise TurnCancelled

    def _add_part(self, part: dict[str, Any]) -> int:
        content = self._content()
        content.append(part)
        position = len(content) - 1
        self._send([{"type": "set", "path": [*self._path, "content", position], "value": part}])
        return position

    def start(self) -> None:
        self._content()

    def replay(self, message: dict[str, Any]) -> None:
        self.message = copy.deepcopy(message)
        self._send([{"type": "set", "path": self._path, "value": self.message}])

    def reasoning_delta(self, delta: str) -> None:
        self._check_cancelled()
        content = self._content()
        if not content or content[-1]["type"] != "reasoning":
            self._add_part({"type": "reasoning", "text": ""})
        position = len(content) - 1
        content[position]["text"] += delta
        self._send(
            [
                {
                    "type": "append-text",
                    "path": [*self._path, "content", position, "text"],
                    "value": delta,
                }
            ]
        )

    def step_started(
        self, step: StepName | Literal["purchase_update"], args: dict[str, Any]
    ) -> None:
        self._check_cancelled()
        self._open_step = self._add_part(
            {
                "type": "tool-call",
                "toolCallId": self._new_id("call"),
                "toolName": step,
                "args": args,
                "argsText": json.dumps(args, ensure_ascii=False),
            }
        )

    def step_finished(
        self, step: StepName | Literal["purchase_update"], result: dict[str, Any]
    ) -> None:
        del step
        self._close_step(result, is_error=False)

    def step_failed(self, step: StepName, error: dict[str, str]) -> None:
        del step
        self._close_step({"error": error}, is_error=True)

    def _close_step(self, result: dict[str, Any], *, is_error: bool) -> None:
        position = self._open_step
        if position is None:
            return
        self._open_step = None
        part = self._content()[position]
        part["result"] = result
        part["isError"] = is_error
        path = [*self._path, "content", position]
        self._send(
            [
                {"type": "set", "path": [*path, "result"], "value": result},
                {"type": "set", "path": [*path, "isError"], "value": is_error},
            ]
        )

    def text(self, text: str) -> None:
        position = self._add_part({"type": "text", "text": ""})
        self._content()[position]["text"] = text
        self._send(
            [
                {
                    "type": "append-text",
                    "path": [*self._path, "content", position, "text"],
                    "value": text,
                }
            ]
        )

    def set_purchase(self, view: dict[str, Any] | None) -> None:
        self._send([{"type": "set", "path": ["purchase"], "value": view}])

    def _finish(self, status: dict[str, Any]) -> None:
        self._content()
        assert self.message is not None
        self.message["status"] = status
        self._send([{"type": "set", "path": [*self._path, "status"], "value": status}])

    def complete(self) -> None:
        self._finish({"type": "complete", "reason": "stop"})

    def fail(self, code: str | None) -> None:
        message = error_message(code)
        self._close_step(
            {"error": {"code": code or "agent_error", "message": message}}, is_error=True
        )
        self._finish({"type": "incomplete", "reason": "error", "error": message})

    def cancel(self) -> None:
        self._close_step({"error": {"code": "cancelled", "message": CANCELLED_STEP}}, is_error=True)
        self._finish({"type": "incomplete", "reason": "cancelled"})


def apply_operations(controller: RunController, operations: list[Operation]) -> None:
    """Apply recorder operations through the public state proxy, then flush them as one batch."""
    for operation in operations:
        path = operation["path"]
        value = operation["value"]
        if operation["type"] == "append-text":
            controller.append_state_text(path, value)
            continue
        if not path:
            controller.state = value
            continue
        parent = controller.state
        for key in path[:-1]:
            parent = parent[key]
        last = path[-1]
        if isinstance(last, int) and last == len(parent):
            parent.append(value)
        else:
            parent[last] = value
    controller.flush()


def _error_code(error: BaseException) -> str | None:
    if isinstance(error, ProductFeedError):
        return "catalog_unavailable"
    code = getattr(error, "code", None)
    return code if isinstance(code, str) else None


class AssistantTransport:
    def __init__(self, service: ConversationService, *, new_id: IdFactory) -> None:
        self._service = service
        self._new_id = new_id

    async def run(
        self, controller: RunController, request: AssistantRequest, owner_id: str = "local-human"
    ) -> None:
        """Run callback for `create_run`. It never raises, so no raw error reaches the client."""
        recorder: AssistantMessageRecorder | None = None
        try:
            messages, claimed_run_id = sanitize_state(request.state, self._new_id)
            purchase = await asyncio.to_thread(
                self._restore_state, messages, claimed_run_id, owner_id
            )
            controller.state = {"messages": messages, "purchase": purchase}
            controller.flush()
            add_messages = 0
            for command in request.commands:
                if isinstance(command, AddMessageCommand):
                    assert request.details is not None
                    key = (
                        request.turn_id
                        if add_messages == 0
                        else f"{request.turn_id}:{add_messages}"
                    )
                    add_messages += 1
                    await self._add_message(
                        controller, command, request.details, key, request.conversation_id, owner_id
                    )
                elif isinstance(command, PurchaseUpdatedCommand):
                    recorder = self._direct_recorder(controller)
                    await self._purchase_updated(recorder, command.runId, owner_id)
                    recorder = None
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("Assistant run failed: %s", type(error).__name__)
            try:
                failed = recorder or self._direct_recorder(controller)
                failed.fail(_error_code(error))
            except Exception:
                logger.warning("Assistant run could not record its failure")

    def _direct_recorder(self, controller: RunController) -> AssistantMessageRecorder:
        if controller.state is None:
            controller.state = {"messages": [], "purchase": None}
        index = len(controller.state["messages"])
        return AssistantMessageRecorder(
            index=index,
            new_id=self._new_id,
            emit=lambda operations: apply_operations(controller, operations),
        )

    def _verified_purchase(self, run_id: str | None, owner_id: str) -> dict[str, Any] | None:
        return self._service.purchase_view(run_id, owner_id) if run_id is not None else None

    def _restore_state(
        self, messages: list[dict[str, Any]], claimed_run_id: str | None, owner_id: str
    ) -> dict[str, Any] | None:
        """Reemplaza por datos de SQLite lo que el cliente devolvió y la UI usa como autoritativo.

        Corre en un hilo porque toca la base. El `run_id` que declara el cliente solo se usa como
        pista: si no señala ninguna compra, se muestra la pendiente que haya en SQLite.
        """
        self._restore_approval_parts(messages, owner_id)
        return self._verified_purchase(
            claimed_run_id, owner_id
        ) or self._service.pending_purchase_view(owner_id)

    def _restore_approval_parts(self, messages: list[dict[str, Any]], owner_id: str) -> None:
        """Repinta cada `request_approval` con los términos guardados, no con los del cliente."""
        parts = [
            part
            for message in messages
            for part in message["content"]
            if part.get("toolName") == "request_approval"
            and part.get("isError") is not True
            and "result" in part
        ]
        run_ids = sorted({run_id for part in parts if (run_id := _approval_run_id(part))})
        snapshots = self._service.approval_terms_snapshots(run_ids, owner_id)
        for part in parts:
            run_id = _approval_run_id(part)
            view = self._verified_purchase(run_id, owner_id)
            terms = snapshots.get(run_id) if run_id is not None else None
            if run_id is None or view is None or terms is None:
                part["result"] = {
                    "error": {
                        "code": "purchase_not_found",
                        "message": ERROR_MESSAGES["purchase_not_found"],
                    }
                }
                part["isError"] = True
                continue
            part["args"] = {"run_id": run_id}
            part["argsText"] = json.dumps(part["args"], ensure_ascii=False)
            part["result"] = {"run_id": run_id, "stage": view["stage"], "terms": terms}
            part["isError"] = False

    async def _purchase_updated(
        self, recorder: AssistantMessageRecorder, run_id: str, owner_id: str
    ) -> None:
        recorder.step_started("purchase_update", {"run_id": run_id})
        view = await asyncio.to_thread(self._service.purchase_view, run_id, owner_id)
        if view is None:
            recorder.fail("purchase_not_found")
            return
        recorder.step_finished("purchase_update", view)
        recorder.text(purchase_update_text(view))
        recorder.set_purchase(view)
        recorder.complete()

    async def _add_message(
        self,
        controller: RunController,
        command: AddMessageCommand,
        details: CheckoutDetails,
        key: str,
        conversation_id: str | None,
        owner_id: str,
    ) -> None:
        text = command.message.text
        state = controller.state
        display: list[dict[str, Any]] = list(state["messages"]) if state is not None else []
        history = model_history(display, text)
        user_message = {
            "id": self._new_id("msg"),
            "role": "user",
            "content": [{"type": "text", "text": text}],
        }
        apply_operations(
            controller,
            [{"type": "set", "path": ["messages", len(display)], "value": user_message}],
        )
        fingerprint = request_sha256(
            {
                "endpoint": "assistant",
                "messages": [message.model_dump(mode="json") for message in history],
                "details": details.model_dump(mode="json"),
                "conversation_id": conversation_id,
            }
        )
        index = len(display) + 1
        await self._pump(
            controller,
            lambda emit, cancelled: self._turn_worker(
                emit,
                cancelled,
                index,
                history,
                details,
                key,
                fingerprint,
                conversation_id,
                owner_id,
            ),
        )

    async def _pump(
        self,
        controller: RunController,
        work: Callable[[Emit, Callable[[], bool]], None],
    ) -> None:
        """Run blocking work in a thread and apply its operations on the event loop in order."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[list[Operation] | None] = asyncio.Queue()
        stop = threading.Event()

        def post(item: list[Operation] | None) -> None:
            if item is not None and stop.is_set():
                return  # The client is gone; only the end-of-work sentinel still matters.
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:
                stop.set()  # The event loop is closed.

        def target() -> None:
            try:
                work(post, stop.is_set)
            finally:
                post(None)

        worker = loop.run_in_executor(None, target)
        try:
            while (operations := await queue.get()) is not None:
                if controller.is_cancelled:
                    stop.set()
                apply_operations(controller, operations)
            await worker
        except asyncio.CancelledError:
            stop.set()
            raise

    def _turn_worker(
        self,
        emit: Emit,
        cancelled: Callable[[], bool],
        index: int,
        history: tuple[ConversationMessage, ...],
        details: CheckoutDetails,
        key: str,
        fingerprint: str,
        conversation_id: str | None,
        owner_id: str,
    ) -> None:
        recorder = AssistantMessageRecorder(
            index=index, new_id=self._new_id, emit=emit, cancelled=cancelled
        )
        service = self._service

        def precheck() -> dict[str, Any] | None:
            # La compra pendiente se busca en SQLite: el cliente no elige cuál se verifica.
            guarded = service.pending_purchase_guard(owner_id)
            if guarded is None:
                return None
            view, response = guarded
            recorder.text(response.assistant_message)
            recorder.complete()
            return {"message": recorder.message, "run_id": view["run_id"]}

        def compute() -> dict[str, Any]:
            recorder.start()
            response = service.execute(
                history,
                details,
                turn_id=key,
                conversation_id=conversation_id,
                observer=recorder,
                owner_id=owner_id,
            )
            run_id = None
            text = response.assistant_message
            if response.purchase is not None:
                run_id = response.purchase.run_id
                # La tarjeta de aprobación ya pide revisar los términos; el texto no lo repite.
                if response.purchase.price_changed:
                    text = f"{text}\n\n{PRICE_CHANGED_NOTE}"
            recorder.text(text)
            if run_id is not None:
                recorder.set_purchase(service.purchase_view(run_id, owner_id))
            recorder.complete()
            return {"message": recorder.message, "run_id": run_id}

        try:
            payload, replayed = service.run_idempotent(
                key, fingerprint, compute, precheck=precheck, owner_id=owner_id
            )
            if replayed:
                recorder.replay(payload["message"])
                run_id = payload.get("run_id")
                if isinstance(run_id, str):
                    recorder.set_purchase(service.purchase_view(run_id, owner_id))
        except TurnCancelled:
            recorder.cancel()
        except Exception as error:
            logger.warning(
                "Assistant turn failed: %s (%s)", type(error).__name__, _error_code(error)
            )
            recorder.fail(_error_code(error))


def _rejection(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"code": code, "message": message}, status_code=status)


def register_assistant_transport(
    app: FastAPI,
    *,
    service: ConversationService,
    new_id: IdFactory = default_id_factory,
    current_buyer: Callable[..., BuyerPrincipal] | None = None,
) -> AssistantTransport:
    transport = AssistantTransport(service, new_id=new_id)

    def local_buyer() -> BuyerPrincipal:
        return BuyerPrincipal("local-human")

    @app.post("/assistant")
    async def assistant(
        request: Request,
        buyer: Annotated[BuyerPrincipal, Depends(current_buyer or local_buyer)],
    ) -> Response:
        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY_BYTES):
            return _rejection(413, "request_too_large", "La solicitud es demasiado grande.")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY_BYTES:
                return _rejection(413, "request_too_large", "La solicitud es demasiado grande.")
        try:
            parsed = AssistantRequest.model_validate_json(bytes(body))
        except ValidationError:
            # ValidationError details may echo client input, so they are not returned.
            return _rejection(422, "invalid_request", "La solicitud no es válida.")

        async def callback(controller: RunController) -> None:
            await transport.run(controller, parsed, buyer.id)

        return AssistantTransportResponse(create_run(callback, state=None))

    return transport
