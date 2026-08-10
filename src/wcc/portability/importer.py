"""Reading a portable capability file, suspiciously (specification 7.13).

Specification 7.13 lists six checks and then says the sentence the whole module
exists for: *imported JSON is never executable merely because it was successfully
parsed*. An import is not a deserialization — it is an admission decision about a
document someone else wrote, which will later be pointed at a real page and run.

Four of the six checks happen here, at read time:

1. JSON and schema validation
2. schema-version compatibility
3. action-allowlist validation
4. origin and route-pattern validation

The last two — deterministic risk recomputation and locator re-resolution — are
deliberately *not* here. They cannot be: both are questions about a live page, and
the answer at import time would be a guess about a page nobody has opened. They
happen where they can be answered, at bind time, and an imported template goes
through exactly the same binder as a compiled one. That is what makes the
guarantee real rather than a promise this module makes on someone else's behalf.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError

from wcc.models.capability import CapabilityTemplate, RiskLevel
from wcc.models.common import SCHEMA_VERSION, ActionType
from wcc.portability.exporter import FILE_FORMAT, CapabilityFile

#: The actions specification 10.4 allows. A plan step naming anything else is
#: refused outright rather than skipped: a plan is a sequence, and dropping a step
#: from someone else's sequence produces a capability that does something other
#: than what its description says.
ALLOWED_ACTIONS = frozenset(ActionType)


class ImportRefused(Exception):
    """The file will not be imported, and why.

    One exception type with a specific message rather than a type per check: a
    caller cannot do anything different about a bad origin than about a bad
    action, and the message is what a person needs to fix the file.
    """


def read_file(path: Path) -> CapabilityFile:
    """Steps 1 and 2: it is JSON, it is the right shape, it is a version we read."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as unreadable:
        raise ImportRefused(f"cannot read {path}: {unreadable}") from unreadable

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as malformed:
        raise ImportRefused(f"{path} is not valid JSON: {malformed}") from malformed

    if not isinstance(payload, dict):
        raise ImportRefused(f"{path} is not a capability file: the top level is not an object")

    declared = payload.get("format")
    if declared != FILE_FORMAT:
        raise ImportRefused(
            f"{path} declares format {declared!r}; this build reads {FILE_FORMAT!r}"
        )

    stored = payload.get("schema_version")
    if stored != SCHEMA_VERSION:
        raise ImportRefused(
            f"{path} holds schema {stored!r} and this build reads {SCHEMA_VERSION!r}. "
            "No migration is defined, so it is refused rather than read with the "
            "wrong meaning"
        )

    try:
        return CapabilityFile.model_validate(payload)
    except ValidationError as invalid:
        raise ImportRefused(f"{path} does not match the capability file schema: {invalid}") from (
            invalid
        )


def check_template(template: CapabilityTemplate, *, file_origin: str) -> None:
    """Steps 3 and 4, plus the two refusals a stored plan makes possible.

    The action allowlist is the load-bearing one. Everything else in a template is
    a description; the plan is the part that acts, and a plan is exactly as
    trustworthy as the person who wrote the file.
    """
    if template.origin != file_origin:
        raise ImportRefused(
            f"template {template.name} claims origin {template.origin} in a file for "
            f"{file_origin}; a file is judged as one thing"
        )

    _check_origin_shape(template.origin, template.name)
    _check_route(template.normalized_route_pattern, template.name)

    for step in template.execution_plan_template:
        if step.action not in ALLOWED_ACTIONS:
            raise ImportRefused(
                f"template {template.name} contains action {step.action!r}, which is not "
                "one of the allowlisted actions of specification 10.4"
            )

    if template.risk.level is not RiskLevel.LOW:
        # Importable and never runnable is a trap: the template would sit in
        # storage, appear in lookups, and be refused at every execution. Refusing
        # it at the door says so once, at the moment someone can act on it.
        raise ImportRefused(
            f"template {template.name} is {template.risk.level} risk and only low-risk "
            "capabilities ever execute; importing it would store something that can "
            "never run"
        )


def _check_origin_shape(origin: str, name: str) -> None:
    """An origin is a scheme and a host, and nothing else.

    A template whose "origin" carries a path, a query, or credentials is not a
    template with an unusual origin — it is a template whose origin field has been
    used for something else, and every comparison against it afterwards is wrong.
    """
    parts = urlsplit(origin)
    if parts.scheme not in {"http", "https"}:
        raise ImportRefused(f"template {name} has origin {origin!r}, which is not http(s)")
    if not parts.hostname:
        raise ImportRefused(f"template {name} has origin {origin!r}, which names no host")
    if parts.path or parts.query or parts.fragment or parts.username or parts.password:
        raise ImportRefused(
            f"template {name} has origin {origin!r}; an origin is scheme and host only"
        )


def _check_route(route: str, name: str) -> None:
    if not route.startswith("/"):
        raise ImportRefused(
            f"template {name} has route pattern {route!r}, which is not an absolute path"
        )
    if "?" in route or "#" in route:
        raise ImportRefused(
            f"template {name} has route pattern {route!r}; a normalized route holds no "
            "query or fragment (specification 7.3)"
        )


def import_file(path: Path) -> list[CapabilityTemplate]:
    """Every template in the file, checked, and marked as someone else's work.

    ``provenance`` is set to ``imported`` here rather than trusted from the file.
    A file claiming ``compiled`` would otherwise launder imported plans into
    locally compiled ones, and specification 17 keeps the distinction precisely so
    that an operator can tell where a stored capability came from.
    """
    capability_file = read_file(path)
    for template in capability_file.templates:
        check_template(template, file_origin=capability_file.origin)
    return [
        template.model_copy(update={"provenance": "imported"})
        for template in capability_file.templates
    ]
