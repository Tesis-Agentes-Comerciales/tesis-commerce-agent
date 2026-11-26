"""The product never imports the evaluation package, and its wheel does not carry it."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_product_package_does_not_import_evaluation() -> None:
    source_root = ROOT / "src" / "commerce_agent"
    violations = []
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        modules += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        if any(module.split(".")[0] == "commerce_evaluation" for module in modules):
            violations.append(path.relative_to(source_root).as_posix())

    assert violations == []
