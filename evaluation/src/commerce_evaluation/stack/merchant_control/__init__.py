"""Merchant fixture control for the live stack. Run ONLY with the Merchant's interpreter.

The harness starts it as `python -m commerce_evaluation.stack.merchant_control` through
`uv run --project $MERCHANT_REPO`, with only this package's source on PYTHONPATH; no Agent
process imports a Merchant package. JSON lines on stdin/stdout carry synthetic fixture
credentials in memory only. Failures report the exception type, never values, rows, DSNs or
tokens. Every module here imports the standard library only.
"""
