"""Fixtures of the opt-in live stack. Nothing starts unless a RUN_LIVE_STACK test runs."""

import pytest

from tests.integration.live_stack import LiveStack, missing_requirements


@pytest.fixture(scope="session")
def stack(tmp_path_factory: pytest.TempPathFactory):
    if problems := missing_requirements():
        pytest.fail("RUN_LIVE_STACK=1 also requires " + "; ".join(problems), pytrace=False)
    live = LiveStack(tmp_path_factory.mktemp("live-stack"))
    try:
        live.start()
        yield live
    finally:
        live.close()


@pytest.fixture
def proxy(stack: LiveStack):
    """The stack's proxy, transparent and with an empty call log for this test."""
    stack.proxy.reset()
    yield stack.proxy
    errors = list(stack.proxy.errors)
    blocked = [(call.method, call.path) for call in stack.proxy.calls if call.fault == "blocked"]
    stack.proxy.reset()
    assert not errors, errors
    assert not blocked, blocked
