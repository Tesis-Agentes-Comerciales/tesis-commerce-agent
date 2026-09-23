import json
from pathlib import Path

import pytest

from commerce_agent.acp import ProductFeedError, load_product_feed

FIXTURE = Path(__file__).parent / "fixtures" / "feed"


def test_loads_schema_valid_feed_and_projects_candidates() -> None:
    feed = load_product_feed(FIXTURE)

    assert feed.metadata.id == "feed_test_headphones"
    assert len(feed.products) == 2
    assert [candidate.variant_id for candidate in feed.candidates] == ["SON-01", "SON-07"]
    assert [candidate.variant_id for candidate in feed.available_candidates] == ["SON-01"]

    candidate = feed.candidate("SON-01")
    assert candidate is not None
    assert candidate.product_id == "prod-son-01"
    assert candidate.observed_unit_amount_minor == 72_000
    assert candidate.currency == "COP"
    assert candidate.categories == ("headphones",)
    assert candidate.conditions == ("new",)
    assert candidate.options == (("Color", "black"),)


def test_feed_price_is_explicitly_observed_not_authoritative() -> None:
    candidate = load_product_feed(FIXTURE).candidate("SON-01")
    assert candidate is not None
    assert "observed_unit_amount_minor" in candidate.model_json_schema()["properties"]
    assert "total" not in candidate.model_dump()


def test_rejects_missing_feed_files(tmp_path: Path) -> None:
    with pytest.raises(ProductFeedError) as captured:
        load_product_feed(tmp_path)
    assert captured.value.code == "missing_feed_file"


def test_rejects_invalid_product_without_echoing_content(tmp_path: Path) -> None:
    secret_marker = "never-echo-this-token"
    (tmp_path / "metadata.json").write_bytes((FIXTURE / "metadata.json").read_bytes())
    (tmp_path / "products.jsonl").write_text(
        json.dumps({"id": secret_marker, "variants": [{"id": "missing-title"}]}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ProductFeedError) as captured:
        load_product_feed(tmp_path)
    assert captured.value.code == "invalid_product"
    assert captured.value.line == 1
    assert secret_marker not in str(captured.value)


def test_rejects_duplicate_variant_ids_across_products(tmp_path: Path) -> None:
    (tmp_path / "metadata.json").write_bytes((FIXTURE / "metadata.json").read_bytes())
    lines = (FIXTURE / "products.jsonl").read_text(encoding="utf-8").splitlines()
    second = json.loads(lines[1])
    second["variants"][0]["id"] = "SON-01"
    (tmp_path / "products.jsonl").write_text(
        lines[0] + "\n" + json.dumps(second) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ProductFeedError) as captured:
        load_product_feed(tmp_path)
    assert captured.value.code == "duplicate_variant_id"
    assert captured.value.line == 2


def test_rejects_empty_lines_to_keep_jsonl_deterministic(tmp_path: Path) -> None:
    (tmp_path / "metadata.json").write_bytes((FIXTURE / "metadata.json").read_bytes())
    (tmp_path / "products.jsonl").write_text(
        (FIXTURE / "products.jsonl").read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ProductFeedError) as captured:
        load_product_feed(tmp_path)
    assert captured.value.code == "empty_product_line"
    assert captured.value.line == 3
