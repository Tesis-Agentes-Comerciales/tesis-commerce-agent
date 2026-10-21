"""The AP2 receipt profile both verifying clients share (docs/ap2-profile.md, Recibos).

`commerce_agent.ap2_receipt` is a leaf: it imports only the AP2 SDK and the standard library, so
the PSP client and the merchant client each load first, in a fresh interpreter, without a cycle
through `commerce_agent.acp`.
"""

import subprocess
import sys

import pytest

VERIFIERS = ("commerce_agent.delegated_payment", "commerce_agent.acp.client")


def fresh_interpreter(code: str) -> subprocess.CompletedProcess[str]:
    # `-I`: nothing preloaded and no PYTHONPATH, as a script whose imports run in any order.
    return subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)


@pytest.mark.parametrize("module", VERIFIERS)
def test_each_receipt_verifier_imports_first_on_its_own(module: str) -> None:
    loaded = fresh_interpreter(f"import {module}")

    assert loaded.returncode == 0, loaded.stderr


def test_the_receipt_profile_loads_nothing_else_of_the_product() -> None:
    loaded = fresh_interpreter(
        "import sys, commerce_agent.ap2_receipt\n"
        "print(sorted(m for m in sys.modules if m.startswith('commerce_agent.')))"
    )

    assert loaded.returncode == 0, loaded.stderr
    assert loaded.stdout.strip() == "['commerce_agent.ap2_receipt']"
