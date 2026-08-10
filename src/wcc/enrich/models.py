"""What a provider is allowed to say back (specification 9.3).

"Output is parsed into a Pydantic model; invalid output is rejected, not repaired."
That sentence decides the shape of this module. Every field is validated at
construction, ``extra="forbid"`` is inherited from :class:`WCCModel`, and there is
no coercion step anywhere — a name that is not ``snake_case`` is not lowercased
into one, and a response naming a candidate that was never sent is not dropped
quietly from an otherwise-accepted response.

Repair is refused because the response is untrusted. The payload that produced it
contains page-authored text, so a malformed field is as likely to be the page
talking as the model slipping, and "fix it up and use it" is how a page's wording
becomes a capability's identity.
"""

from __future__ import annotations

import re

from pydantic import Field, field_validator

from wcc.models.capability import RiskLevel
from wcc.models.common import WCCModel

#: Specification 9.2: ``snake_case``, beginning with an action verb.
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")

#: The verbs a capability name may begin with. A closed list, because "begins with
#: an action verb" is not checkable against an open vocabulary, and a name starting
#: with a noun reads as a thing rather than as something the agent can do.
ACTION_VERBS = frozenset(
    {
        "apply",
        "browse",
        "clear",
        "filter",
        "find",
        "go",
        "list",
        "navigate",
        "open",
        "refine",
        "reset",
        "search",
        "select",
        "show",
        "sort",
        "submit",
        "view",
    }
)

#: Characters that make a string usable as a selector. A description is prose for a
#: human and for a model; prose does not need these, and specification 10.3 means a
#: selector delivered inside a sentence is still a selector. Phase 3 shipped exactly
#: that mistake, so the check is written here rather than trusted to the payload
#: being clean.
_SELECTOR_CHARACTERS = frozenset("#>[]{}<$")

MAX_DESCRIPTION = 300
MAX_PAGE_TYPE = 60


def _plain(text: str, *, limit: int, described_as: str) -> str:
    stripped = text.strip()
    if not stripped:
        raise ValueError(f"{described_as} is empty")
    if len(stripped) > limit:
        raise ValueError(f"{described_as} is longer than {limit} characters")
    if "\n" in stripped:
        raise ValueError(f"{described_as} spans several lines")
    found = sorted(_SELECTOR_CHARACTERS & set(stripped))
    if found:
        raise ValueError(f"{described_as} contains selector punctuation {found}")
    return stripped


class ParameterEnrichment(WCCModel):
    """A better sentence about one argument — never a different argument.

    There is no ``new_name`` field, and its absence is the design. A parameter's
    name is simultaneously the JSON Schema property, the ``target_ref`` of a plan
    step, and the key of a locator chain, so renaming one rewrites the execution
    plan and the locator map. Phase 6's acceptance forbids enrichment from altering
    either. Specification 9.3 lists "mapping ambiguous labels to parameter names"
    among the permitted uses; this implementation reads that as improving how a
    parameter is *described*, and declines the reading that would let a model
    rewrite a plan.
    """

    name: str
    description: str

    @field_validator("name")
    @classmethod
    def _is_a_parameter_name(cls, value: str) -> str:
        if not NAME_PATTERN.match(value):
            raise ValueError(f"{value!r} is not a parameter name")
        return value

    @field_validator("description")
    @classmethod
    def _is_prose(cls, value: str) -> str:
        return _plain(value, limit=MAX_DESCRIPTION, described_as="a parameter description")


class CapabilityEnrichment(WCCModel):
    """A proposed name, description, and risk hint for one candidate."""

    ref: str
    name: str
    description: str
    parameters: list[ParameterEnrichment] = Field(default_factory=list)
    risk_hint: RiskLevel | None = None
    """A level the provider believes is *higher* than the deterministic one.

    Only ever consulted upward. Specification 10.1 says uncertainty raises and
    never lowers, and a hint that could lower would make the safety of the system
    depend on a model's opinion — which is the one thing a risk model must not do.
    """

    @field_validator("name")
    @classmethod
    def _is_a_capability_name(cls, value: str) -> str:
        if not NAME_PATTERN.match(value):
            raise ValueError(f"{value!r} is not snake_case (specification 9.2)")
        if value.split("_")[0] not in ACTION_VERBS:
            raise ValueError(
                f"{value!r} does not begin with an action verb (specification 9.2); "
                f"expected one of {sorted(ACTION_VERBS)}"
            )
        return value

    @field_validator("description")
    @classmethod
    def _is_prose(cls, value: str) -> str:
        return _plain(value, limit=MAX_DESCRIPTION, described_as="a capability description")


class PageEnrichment(WCCModel):
    """One provider response, covering one page."""

    page_type: str
    capabilities: list[CapabilityEnrichment] = Field(default_factory=list)

    @field_validator("page_type")
    @classmethod
    def _is_a_short_label(cls, value: str) -> str:
        return _plain(value, limit=MAX_PAGE_TYPE, described_as="a page type")

    @field_validator("capabilities")
    @classmethod
    def _each_candidate_at_most_once(
        cls, value: list[CapabilityEnrichment]
    ) -> list[CapabilityEnrichment]:
        refs = [item.ref for item in value]
        if len(set(refs)) != len(refs):
            raise ValueError("a candidate was enriched more than once")
        names = [item.name for item in value]
        if len(set(names)) != len(names):
            raise ValueError("two capabilities were given the same name")
        return value
