"""Argument validation against a capability's JSON Schema (specification 11.1).

Step 4 of the execution protocol validates arguments against the capability schema
with ``additionalProperties: false``, and step 6 only then sends anything to the
driver. So this module is the last place a wrong argument can be stopped before it
reaches a page, which decides how it handles a schema it does not fully understand.

**It refuses.** Every keyword is checked against a known set, and an unrecognised
one fails the validation rather than being ignored. The alternative — validate what
you recognise, skip the rest — is the failure that looks like success: a schema
saying ``{"type": "string", "maxLength": 3}`` would accept a hundred-character
value from a validator that had never heard of ``maxLength``, and the page would be
the thing that found out.

That choice is what makes a hand-written validator defensible here instead of a
general JSON Schema library. The compiler emits a small, closed vocabulary; a
schema outside it is either an imported file (specification 7.13, Phase 5) or a
future compiler change, and both are better served by a loud refusal than by
silent partial checking.
"""

from __future__ import annotations

from typing import Any

#: The keywords a capability schema's root object may use.
_ROOT_KEYWORDS = frozenset({"type", "properties", "required", "additionalProperties", "title"})

#: The keywords one property may use. ``description`` is the label the compiler
#: copied from the page, and carries no constraint.
_PROPERTY_KEYWORDS = frozenset({"type", "description", "enum", "minimum", "maximum", "title"})

#: The JSON types a generated parameter may have. ``array``, ``object``, and
#: ``null`` are absent because no allowlisted action of specification 10.4 accepts
#: one, so a schema demanding one describes a step that cannot be executed.
_SCALAR_TYPES = frozenset({"string", "integer", "number", "boolean"})


class ArgumentError(Exception):
    """The arguments do not satisfy the capability's schema.

    A caller error, and named as one: nothing has been sent to the driver, and
    re-binding would not change the answer.
    """

    code = "invalid_arguments"


class SchemaUnsupported(ArgumentError):
    """The schema uses something this validator does not implement.

    A distinct class because the fault is not the caller's. The arguments may well
    have been fine; what could not be established is that they were.
    """

    code = "unsupported_schema"


def validate_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    """Return ``arguments`` if the schema accepts them, or raise.

    The returned mapping is the validated one, not a coerced one. Coercion here
    would mean deciding that the string ``"2"`` was meant as the integer ``2``, and
    a runtime that repairs its caller's arguments is a runtime whose schema no
    longer describes what it accepts.
    """
    _check_root(schema)
    properties = dict(schema.get("properties") or {})
    required = list(schema.get("required") or [])

    # Every declared property, not only the ones the caller supplied. Checking a
    # property's schema lazily meant an unsupported keyword on an optional
    # parameter was never reached: a capability declaring
    # ``{"x": {"type": "string", "maxLength": 3}}`` accepted ``{}`` and ran, which
    # is the fail-open this module exists to refuse.
    for name, property_schema in properties.items():
        _check_property_schema(name, property_schema)

    unexpected = sorted(set(arguments) - set(properties))
    if unexpected:
        raise ArgumentError(
            f"{unexpected} are not parameters of this capability; the schema sets "
            "additionalProperties: false"
        )

    missing = sorted(name for name in required if name not in arguments)
    if missing:
        raise ArgumentError(f"{missing} are required and were not supplied")

    for name, value in arguments.items():
        _check_value(name, properties[name], value)
    return dict(arguments)


def _check_root(schema: dict[str, Any]) -> None:
    unknown = sorted(set(schema) - _ROOT_KEYWORDS)
    if unknown:
        raise SchemaUnsupported(f"the capability schema uses unsupported keywords {unknown}")
    if schema.get("type") != "object":
        raise SchemaUnsupported(
            f"a capability schema must be an object, not {schema.get('type')!r}"
        )
    if schema.get("additionalProperties") is not False:
        raise SchemaUnsupported(
            "specification 11.1 validates arguments with additionalProperties: false, "
            "and this schema does not declare it"
        )
    properties = schema.get("properties")
    if properties is not None and not isinstance(properties, dict):
        raise SchemaUnsupported("properties must be an object")

    required = schema.get("required")
    if required is not None:
        if not isinstance(required, list) or not all(isinstance(name, str) for name in required):
            raise SchemaUnsupported("required must be a list of names")
        declared = set(properties or {})
        undeclared = sorted(set(required) - declared)
        if undeclared:
            raise SchemaUnsupported(
                f"the schema requires {undeclared}, which it never declares as properties"
            )


def _check_property_schema(name: str, property_schema: Any) -> None:
    """Whether this build can check the constraint at all, said before any value is."""
    if not isinstance(property_schema, dict):
        raise SchemaUnsupported(f"the schema for {name} is not an object")
    unknown = sorted(set(property_schema) - _PROPERTY_KEYWORDS)
    if unknown:
        raise SchemaUnsupported(f"the schema for {name} uses unsupported keywords {unknown}")
    if property_schema.get("type") not in _SCALAR_TYPES:
        raise SchemaUnsupported(
            f"the schema for {name} has unsupported type {property_schema.get('type')!r}"
        )
    allowed = property_schema.get("enum")
    if allowed is not None and not isinstance(allowed, list):
        raise SchemaUnsupported(f"the enum for {name} is not a list")
    for keyword in ("minimum", "maximum"):
        bound = property_schema.get(keyword)
        if bound is None:
            continue
        if not isinstance(bound, int | float) or isinstance(bound, bool):
            raise SchemaUnsupported(f"the {keyword} for {name} is not a number")


def _check_value(name: str, property_schema: Any, value: Any) -> None:
    _check_property_schema(name, property_schema)
    declared = property_schema.get("type")

    if not _matches(declared, value):
        raise ArgumentError(f"{name} must be {declared}, and {type(value).__name__} was supplied")

    allowed = property_schema.get("enum")
    if allowed is not None and not any(_same_json_value(value, option) for option in allowed):
        # ``in`` would use Python equality, where ``1 == True``. An enum of
        # ``[true]`` on an integer parameter has no satisfying value in JSON, and
        # accepting ``1`` for it would be this validator inventing one.
        raise ArgumentError(f"{name} must be one of {allowed}")

    _check_bounds(name, property_schema, value)


def _same_json_value(value: Any, option: Any) -> bool:
    """Equality as JSON means it: a boolean is never a number."""
    if isinstance(value, bool) != isinstance(option, bool):
        return False
    return value == option


def _check_bounds(name: str, property_schema: dict[str, Any], value: Any) -> None:
    for keyword, ok in (
        ("minimum", lambda bound: value >= bound),
        ("maximum", lambda bound: value <= bound),
    ):
        bound = property_schema.get(keyword)
        if bound is None:
            continue
        if not isinstance(value, int | float) or isinstance(value, bool):
            raise SchemaUnsupported(f"{keyword} does not apply to the type of {name}")
        if not ok(bound):
            raise ArgumentError(
                f"{name} must be at {'least' if keyword == 'minimum' else 'most'} {bound}"
            )


def _matches(declared: str, value: Any) -> bool:
    """Whether ``value`` is the JSON type ``declared``.

    ``bool`` is excluded from the numeric types deliberately. Python makes ``True``
    an ``int``, JSON does not, and a capability taking a page number would
    otherwise accept ``True`` and page to 1.
    """
    if declared == "boolean":
        return isinstance(value, bool)
    if isinstance(value, bool):
        return False
    if declared == "integer":
        return isinstance(value, int)
    if declared == "number":
        return isinstance(value, int | float)
    return isinstance(value, str)
