from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

ReasoningListener = Callable[[str], None]

_listener: ContextVar[ReasoningListener | None] = ContextVar("reasoning_listener", default=None)


@contextmanager
def reasoning_listener(listener: ReasoningListener | None) -> Iterator[None]:
    """Route reasoning-summary deltas produced in this context to ``listener``.

    The router protocol stays unchanged: routers that support summaries publish them here and the
    caller that renders them decides where they go. Summaries never flow back into prompts.
    """
    token = _listener.set(listener)
    try:
        yield
    finally:
        _listener.reset(token)


def emit_reasoning(delta: str) -> None:
    listener = _listener.get()
    if listener is not None and delta:
        listener(delta)


def reasoning_requested() -> bool:
    return _listener.get() is not None
