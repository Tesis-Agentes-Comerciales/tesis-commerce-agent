"""Port for structured LLM calls with a spending cap (docs/design/eval-suite.md §5).

No transport here reaches a provider: the fake is offline and the budget guard wraps another
transport. Importing this package never imports a provider SDK.
"""
