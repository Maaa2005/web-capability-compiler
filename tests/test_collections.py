"""Collection identity and ``collection_hash`` (specification 7.3, 8.4)."""

from __future__ import annotations

from wcc.compiler.collections import build_collection, collection_hash, parse_structured_data


def _raw(records: list[dict[str, str]], **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "kind": "list",
        "label": "Results",
        "order_is_meaningful": True,
        "records": [{"ordinal": i, "fields": f} for i, f in enumerate(records)],
    }
    base.update(overrides)
    return base


def test_identity_ignores_whitespace_and_key_order() -> None:
    first = build_collection(_raw([{"title": "Wireless  Earbuds", "price": "4,980"}]))
    second = build_collection(_raw([{"price": "4,980", "title": "Wireless Earbuds"}]))
    assert first.records[0].identity == second.records[0].identity


def test_the_hash_changes_when_a_record_changes() -> None:
    """Acceptance criterion 7."""
    before = build_collection(_raw([{"title": "A"}, {"title": "B"}]))
    after = build_collection(_raw([{"title": "A"}, {"title": "C"}]))
    assert before.collection_hash != after.collection_hash


def test_the_hash_changes_when_the_count_changes() -> None:
    before = build_collection(_raw([{"title": "A"}, {"title": "B"}]))
    after = build_collection(_raw([{"title": "A"}, {"title": "B"}, {"title": "C"}]))
    assert before.collection_hash != after.collection_hash


def test_order_matters_only_when_it_is_declared_to() -> None:
    ordered_a = collection_hash(["a", "b"], order_is_meaningful=True)
    ordered_b = collection_hash(["b", "a"], order_is_meaningful=True)
    assert ordered_a != ordered_b

    unordered_a = collection_hash(["a", "b"], order_is_meaningful=False)
    unordered_b = collection_hash(["b", "a"], order_is_meaningful=False)
    assert unordered_a == unordered_b


def test_the_hash_is_stable_across_repeated_builds() -> None:
    raw = _raw([{"title": "A"}, {"title": "B"}])
    assert len({build_collection(raw).collection_hash for _ in range(5)}) == 1


def test_record_ids_are_opaque_and_unique() -> None:
    collection = build_collection(_raw([{"title": "A"}, {"title": "B"}]))
    ids = [record.record_id for record in collection.records]
    assert all(rid.startswith("rec_") for rid in ids)
    assert len(set(ids)) == 2


def test_empty_fields_are_dropped_from_identity() -> None:
    collection = build_collection(_raw([{"title": "A", "note": "   "}]))
    assert "note" not in collection.records[0].fields


def test_malformed_json_ld_is_skipped_rather_than_fatal() -> None:
    """Page content is untrusted data; a broken block must not fail the snapshot."""
    blocks = ['{"@type": "Product"}', "not json at all", "[1, 2, 3]", '[{"@type": "Offer"}]']
    parsed = parse_structured_data(blocks)
    assert [block["@type"] for block in parsed] == ["Product", "Offer"]
