"""Argument validation (specification 11.1 step 4).

The tests that matter most here are the ones about what the validator does *not*
understand. A validator that quietly ignores an unknown keyword reports success on
a constraint it never checked, and the page is what discovers the difference.
"""

from __future__ import annotations

import pytest

from wcc.runtime.arguments import ArgumentError, SchemaUnsupported, validate_arguments

SEARCH = {
    "type": "object",
    "properties": {
        "keyword": {"type": "string", "description": "Keyword"},
        "in_stock": {"type": "boolean"},
        "page": {"type": "integer", "minimum": 1},
    },
    "required": ["keyword"],
    "additionalProperties": False,
}


def test_a_well_formed_call_passes_through_unchanged() -> None:
    arguments = {"keyword": "earbuds", "in_stock": True, "page": 2}
    assert validate_arguments(SEARCH, arguments) == arguments


def test_a_missing_required_argument_is_refused() -> None:
    with pytest.raises(ArgumentError, match="required"):
        validate_arguments(SEARCH, {"in_stock": True})


def test_an_undeclared_argument_is_refused() -> None:
    with pytest.raises(ArgumentError, match="additionalProperties"):
        validate_arguments(SEARCH, {"keyword": "earbuds", "sort": "price"})


def test_a_wrong_type_is_refused_rather_than_coerced() -> None:
    with pytest.raises(ArgumentError, match="must be string"):
        validate_arguments(SEARCH, {"keyword": 7})


def test_true_is_not_an_integer() -> None:
    """Python makes ``True`` an ``int``; JSON does not, and a page number is not a flag."""
    with pytest.raises(ArgumentError, match="must be integer"):
        validate_arguments(SEARCH, {"keyword": "earbuds", "page": True})


def test_a_bound_is_enforced() -> None:
    with pytest.raises(ArgumentError, match="at least 1"):
        validate_arguments(SEARCH, {"keyword": "earbuds", "page": 0})


def test_an_enum_is_enforced() -> None:
    schema = {
        "type": "object",
        "properties": {"sort": {"type": "string", "enum": ["price", "name"]}},
        "additionalProperties": False,
    }
    assert validate_arguments(schema, {"sort": "price"}) == {"sort": "price"}
    with pytest.raises(ArgumentError, match="one of"):
        validate_arguments(schema, {"sort": "rating"})


def test_a_keyword_this_build_cannot_check_fails_the_validation() -> None:
    """The whole reason a hand-written validator is defensible."""
    schema = {
        "type": "object",
        "properties": {"keyword": {"type": "string", "maxLength": 3}},
        "additionalProperties": False,
    }
    with pytest.raises(SchemaUnsupported, match="maxLength"):
        validate_arguments(schema, {"keyword": "earbuds"})


def test_a_schema_that_does_not_close_itself_is_refused() -> None:
    schema = {"type": "object", "properties": {"keyword": {"type": "string"}}}
    with pytest.raises(SchemaUnsupported, match="additionalProperties"):
        validate_arguments(schema, {"keyword": "earbuds"})


def test_a_parameter_shaped_like_a_list_is_refused() -> None:
    """No allowlisted action of specification 10.4 accepts an array."""
    schema = {
        "type": "object",
        "properties": {"tags": {"type": "array"}},
        "additionalProperties": False,
    }
    with pytest.raises(SchemaUnsupported, match="unsupported type"):
        validate_arguments(schema, {"tags": ["a"]})


def test_a_schema_requiring_something_it_never_declares_is_refused() -> None:
    schema = {
        "type": "object",
        "properties": {"keyword": {"type": "string"}},
        "required": ["keyword", "ghost"],
        "additionalProperties": False,
    }
    with pytest.raises(SchemaUnsupported, match="ghost"):
        validate_arguments(schema, {"keyword": "earbuds"})
