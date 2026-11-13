"""Port for structured LLM calls with a spending cap and a usage ledger (eval-suite.md §5).

Only ``claude`` imports the provider SDK; the port, the budget, the batch interface and their
fakes stay offline.
"""
