"""Check loaded local functions against the captured source, without executing archives."""

import ast
import importlib
from collections.abc import Callable
from types import CodeType, FunctionType

from commerce_agent.acp.feed import ProductFeed
from commerce_agent.domain import PurchaseIntent, SelectionResult

from .evaluator import GovernanceError


def verify_loaded_selection(
    manifest: dict[str, str], ranking: Callable[[ProductFeed, PurchaseIntent], SelectionResult]
) -> None:
    def code_map(code: CodeType) -> dict[str, CodeType]:
        result = {code.co_qualname: code}
        for child in code.co_consts:
            if isinstance(child, CodeType):
                result.update(code_map(child))
        return result

    selection = importlib.import_module("commerce_agent.domain.selection")
    if ranking is not selection.rank_candidates:
        raise GovernanceError("GOV_ARTIFACT_MISMATCH")
    for path, name in (
        ("src/commerce_agent/domain/intent.py", "commerce_agent.domain.intent"),
        ("src/commerce_agent/domain/selection.py", "commerce_agent.domain.selection"),
        ("src/commerce_agent/acp/feed.py", "commerce_agent.acp.feed"),
    ):
        module = importlib.import_module(name)
        functions: list[FunctionType] = []
        # Enumerate expected definitions, so a foreign replacement cannot silently
        # disappear from the check just because its __module__ differs.
        for node in ast.parse(manifest[path]).body:
            if isinstance(node, ast.FunctionDef):
                value = vars(module).get(node.name)
                if not isinstance(value, FunctionType):
                    raise GovernanceError("GOV_ARTIFACT_MISMATCH")
                functions.append(value)
            elif isinstance(node, ast.ClassDef):
                value = vars(module).get(node.name)
                if not isinstance(value, type):
                    raise GovernanceError("GOV_ARTIFACT_MISMATCH")
                for definition in node.body:
                    if not isinstance(definition, ast.FunctionDef):
                        continue
                    member = vars(value).get(definition.name)
                    member = (
                        member.__func__
                        if isinstance(member, (classmethod, staticmethod))
                        else member
                    )
                    member = member.fget if isinstance(member, property) else member
                    if not isinstance(member, FunctionType):
                        raise GovernanceError("GOV_ARTIFACT_MISMATCH")
                    functions.append(member)
        if not functions:
            raise GovernanceError("GOV_ARTIFACT_MISMATCH")
        compiled = code_map(
            compile(manifest[path], functions[0].__code__.co_filename, "exec", dont_inherit=True)
        )
        if any(compiled.get(fn.__qualname__) != fn.__code__ for fn in functions):
            raise GovernanceError("GOV_ARTIFACT_MISMATCH")
