"""Fixtures of the opt-in live stack. Nothing starts unless a RUN_LIVE_STACK test runs."""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from tests.integration.live_purchase import PurchaseLane, hnp_requirements, payment_requirements
from tests.integration.live_stack import FaultProxy, LiveStack, missing_requirements


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


@contextmanager
def checked(proxy: FaultProxy) -> Iterator[FaultProxy]:
    """The proxy, transparent and with an empty call log for one test, verified after it."""
    proxy.reset()
    yield proxy
    errors = list(proxy.errors)
    blocked = [(call.method, call.path) for call in proxy.calls if call.fault == "blocked"]
    proxy.reset()
    assert not errors, errors
    assert not blocked, blocked


@pytest.fixture
def proxy(stack: LiveStack):
    with checked(stack.proxy) as transparent:
        yield transparent


def lane(stack: LiveStack, profile: str) -> Iterator[PurchaseLane]:
    if problems := payment_requirements():
        pytest.fail("Live purchases also require " + "; ".join(problems), pytrace=False)
    started = PurchaseLane(stack, profile)
    try:
        started.start()
        yield started
    finally:
        started.close()


@pytest.fixture(scope="session")
def b0_lane(stack: LiveStack):
    yield from lane(stack, "b0")


@pytest.fixture(scope="session")
def acp_lane(stack: LiveStack):
    yield from lane(stack, "acp")


@pytest.fixture
def b0(b0_lane: PurchaseLane):
    """B0: ACP + AP2 Human Present against the real Merchant and payment provider."""
    with checked(b0_lane.proxy):
        yield b0_lane


@pytest.fixture
def acp(acp_lane: PurchaseLane):
    """Plain ACP completion (AP2 off) against the real Merchant and payment provider."""
    with checked(acp_lane.proxy):
        yield acp_lane


@pytest.fixture(scope="session")
def hnp_lane(stack: LiveStack):
    if problems := hnp_requirements(stack.merchant):
        pytest.skip("Human Not Present needs " + "; ".join(problems))
    yield from lane(stack, "hnp")


@pytest.fixture
def hnp(hnp_lane: PurchaseLane):
    """Human Not Present: the B0 Merchant and provider, and an HNP Agent per episode. Both
    proxies (Merchant and provider) start transparent and are checked after the test."""
    assert hnp_lane.psp_proxy is not None
    with checked(hnp_lane.proxy), checked(hnp_lane.psp_proxy):
        yield hnp_lane
