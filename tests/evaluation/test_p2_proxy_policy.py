"""Proxy isolation controls only: loopback + fake upstream, not commerce validation."""

import importlib.util
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from tests.support.p2_proxy_policy import P2_ADDITIONAL_ROUTES, p2_routes


class P2PolicyTests(unittest.TestCase):
    def test_only_the_two_required_permissions_are_added(self) -> None:
        base = frozenset({("GET", "/existing"), ("POST", "/checkout_sessions")})
        expected = frozenset({("GET", "/feeds/current"), ("POST", "/checkout_sessions/*/cancel")})
        self.assertEqual(p2_routes(base) - base, expected)
        self.assertEqual(P2_ADDITIONAL_ROUTES, expected)

    def test_base_policy_is_preserved_without_mutation(self) -> None:
        base = frozenset({("GET", "/existing")})
        configured = p2_routes(base)
        self.assertEqual(base, frozenset({("GET", "/existing")}))
        self.assertTrue(base <= configured)
        self.assertIsNot(base, configured)

    def test_configuring_twice_does_not_expand_the_scope(self) -> None:
        configured = p2_routes(frozenset())
        self.assertEqual(p2_routes(configured), configured)


@contextmanager
def running_proxy(
    routes: frozenset[tuple[str, str]],
) -> Iterator[tuple[Any, list[tuple[str, str]]]]:
    import httpx

    from tests.integration.live_stack import FaultProxy

    forwarded: list[tuple[str, str]] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        forwarded.append((request.method, request.url.path))
        # Stream explicitly: FaultProxy reads raw streaming bytes as with a real server.
        return httpx.Response(204, stream=httpx.ByteStream(b""))

    proxy = FaultProxy(routes)
    try:
        proxy.client.close()
        proxy.client = httpx.Client(transport=httpx.MockTransport(upstream), trust_env=False)
        proxy.upstream = "http://merchant.invalid"
        yield proxy, forwarded
    finally:
        proxy.close()


@unittest.skipUnless(
    all(importlib.util.find_spec(name) for name in ("httpx", "pydantic", "pytest", "fastapi")),
    "requires this checkout's locked dependencies; executed in CI",
)
class P2HTTPProxyControls(unittest.TestCase):
    def test_original_acp_policy_blocks_both_routes_and_maps_cancel_405_to_502(self) -> None:
        import httpx
        from pydantic import SecretStr

        from commerce_agent.acp import ACPClient, ACPClientError
        from commerce_agent.api import error_response
        from tests.integration.live_purchase import PURCHASE_ROUTES

        with running_proxy(PURCHASE_ROUTES) as (proxy, forwarded):
            with httpx.Client(trust_env=False) as client:
                response = client.get(proxy.origin + "/feeds/current")
            self.assertEqual(response.status_code, 405)
            with (
                ACPClient(proxy.origin, SecretStr("synthetic-proxy-control")) as gateway,
                self.assertRaises(ACPClientError) as failure,
            ):
                gateway.cancel_checkout("cs_control", idempotency_key="control-only")
            self.assertEqual(failure.exception.status_code, 405)
            self.assertEqual(error_response(failure.exception).status_code, 502)
            self.assertEqual(forwarded, [])
            self.assertTrue(all(call.fault == "blocked" for call in proxy.calls))
            self.assertTrue(all(not call.answered.is_set() for call in proxy.calls))

    def test_p2_allows_exactly_the_required_additions_and_preserves_baseline(self) -> None:
        import httpx

        from tests.integration.live_purchase import PURCHASE_ROUTES
        from tests.integration.live_stack import route_allowed

        before = frozenset(PURCHASE_ROUTES)
        configured = p2_routes(PURCHASE_ROUTES)
        self.assertEqual(configured - before, P2_ADDITIONAL_ROUTES)
        for method, template in before:
            self.assertTrue(route_allowed(configured, method, template.replace("*", "id")))
        with running_proxy(configured) as (proxy, forwarded):
            with httpx.Client(trust_env=False) as client:
                self.assertEqual(client.get(proxy.origin + "/feeds/current").status_code, 204)
                self.assertEqual(
                    client.post(proxy.origin + "/checkout_sessions/cs_control/cancel").status_code,
                    204,
                )
            self.assertEqual(
                forwarded,
                [("GET", "/feeds/current"), ("POST", "/checkout_sessions/cs_control/cancel")],
            )
            self.assertTrue(all(call.fault == "pass" for call in proxy.calls))
            self.assertTrue(all(call.answered.is_set() for call in proxy.calls))
        self.assertEqual(PURCHASE_ROUTES, before)

    def test_p2_unauthorized_routes_still_return_http_405_without_forwarding(self) -> None:
        import httpx

        from tests.integration.live_purchase import PURCHASE_ROUTES

        foreign = [
            ("GET", "/admin"),
            ("POST", "/feeds/current"),
            ("GET", "/checkout_sessions/cs_control/cancel"),
            ("POST", "/checkout_sessions/cs_control/cancel/extra"),
            ("GET", "/feeds/current/extra"),
            ("POST", "/lab/offers/delete"),
        ]
        with running_proxy(p2_routes(PURCHASE_ROUTES)) as (proxy, forwarded):
            with httpx.Client(trust_env=False) as client:
                for method, path in foreign:
                    with self.subTest(method=method, path=path):
                        self.assertEqual(
                            client.request(method, proxy.origin + path).status_code, 405
                        )
            self.assertEqual(forwarded, [])
            self.assertEqual(len(proxy.calls), len(foreign))
            self.assertTrue(all(call.fault == "blocked" for call in proxy.calls))
            self.assertTrue(all(not call.answered.is_set() for call in proxy.calls))


if __name__ == "__main__":
    unittest.main()
