from pathlib import Path


def test_buyer_agent_package_does_not_import_merchant_package() -> None:
    source_root = Path(__file__).resolve().parents[2] / "src" / "commerce_agent"
    forbidden = ("import commerce_lab", "from commerce_lab")

    violations = [
        path.relative_to(source_root).as_posix()
        for path in source_root.rglob("*.py")
        if any(pattern in path.read_text(encoding="utf-8") for pattern in forbidden)
    ]

    assert violations == []
