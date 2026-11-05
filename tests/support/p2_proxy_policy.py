"""Additional transport permissions of the P2 fixture, never the global purchase lane."""

P2_ADDITIONAL_ROUTES = frozenset(
    {("GET", "/feeds/current"), ("POST", "/checkout_sessions/*/cancel")}
)


def p2_routes(base: frozenset[tuple[str, str]]) -> frozenset[tuple[str, str]]:
    return base | P2_ADDITIONAL_ROUTES
