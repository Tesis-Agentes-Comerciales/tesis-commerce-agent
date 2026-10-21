"""No model-facing tool reaches the HNP signers (spec §3.1, AGENTS.md «Trust boundaries»)."""

import subprocess
import sys
from pathlib import Path

from commerce_agent.llm.catalog_tool import CatalogLookupArgs, make_catalog_lookup
from tests.test_conversation_router import FEED

SOURCE = Path(__file__).resolve().parents[1] / "src" / "commerce_agent"
MODEL_FACING = (
    "commerce_agent.llm.catalog_tool",
    "commerce_agent.llm.conversation_router",
    "commerce_agent.application.conversation",
    # What the HNP selector sees: the shortlist, the intent and the buyer's preference text.
    "commerce_agent.application.hnp_selection",
    "commerce_agent.llm.delegated_choice",
)


def test_model_facing_modules_never_load_the_hnp_signers() -> None:
    probe = (
        "import importlib, sys\n"
        f"for name in {MODEL_FACING!r}:\n"
        "    importlib.import_module(name)\n"
        "print(sorted(m for m in sys.modules if m.endswith('ap2_hnp')))"
    )

    loaded = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )

    assert loaded.stdout.strip() == "[]"


def test_no_model_facing_source_names_the_hnp_signers() -> None:
    model_sources = [
        *(SOURCE / "llm").glob("*.py"),
        SOURCE / "application" / "conversation.py",
        SOURCE / "application" / "hnp_selection.py",
    ]
    signers = ("ap2_hnp", "OpenMandateIssuer", "AgentKeyCloser", "present_closed_mandate")

    assert [
        path.name
        for path in model_sources
        if any(name in path.read_text(encoding="utf-8") for name in signers)
    ] == []


def test_the_only_model_tool_takes_a_query_and_answers_catalog_data() -> None:
    tool = make_catalog_lookup(FEED)

    assert tool.name == "lookup_catalog"
    assert set(CatalogLookupArgs.model_fields) == {"query"}
    output = tool.invoke({"query": "Sonora"})
    for secret_shape in ("BEGIN PRIVATE KEY", '"d":', "~~", "kb+sd-jwt", "cnf"):
        assert secret_shape not in output
