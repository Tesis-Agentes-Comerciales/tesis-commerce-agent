"""EC-HNP-02 model adapters for the offline HNP harness.

Only ``transport.OpenAIResponsesTransport`` can reach a provider, and only when a run opts in
explicitly. Importing this package never imports the OpenAI SDK.
"""
