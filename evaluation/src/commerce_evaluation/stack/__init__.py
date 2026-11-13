"""The live stack the evaluation suite and tests/integration start per run.

A disposable PostgreSQL in Docker, the real Merchant and payment provider from their own
checkouts, FaultProxy in front of them, the Agent processes and the purchase lanes. Opt-in in the
tests (RUN_LIVE_STACK=1 plus MERCHANT_REPO and PAYMENT_REPO; see tests/integration/README.md).

This package imports nothing: `merchant_control` runs under the Merchant's interpreter, which
imports `commerce_evaluation.stack` on the way and has no Agent package to import.
"""
