# Frozen ACP sources

This directory contains the ACP 2026-04-17 sources used to validate the Buyer Agent boundary.
They are copied byte-for-byte from the same frozen bundle used by `tesis-commerce-merchant`.

The active source files are:

- `schema.agentic_checkout.json`: checkout discovery, lifecycle, completion, and order payloads;
- `schema.feed.json`: static Product Feed payloads;
- `openapi.agentic_checkout.yaml`: checkout HTTP surface;
- `openapi.agentic_checkout_webhook.yaml`: order webhook surface;
- `provenance.json`: recorded upstream origin and hashes;
- `LICENSE`: upstream license.

The Buyer Agent must not modify these files to fit its implementation. Local application models may
be narrower, but protocol contract tests validate exchanged payloads against the frozen sources.

