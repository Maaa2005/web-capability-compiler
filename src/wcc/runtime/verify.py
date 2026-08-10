"""Postcondition verification (specification 11.3).

An action that was performed without error is not an action that worked. The click
landed, the page said nothing, and the only honest way to tell the difference is to
look at the page again and compare. That comparison is this module.

Two rules shape it.

**Every signal is computed, not only the expected ones.** The result reports what
was observed as well as what was wanted, because "the URL changed but the result
set did not" is the sentence an operator needs, and a verifier that only ever
answered the question it was asked could not produce it.

**An unrecognised expected signal is never satisfied.** It is reported as missing.
A template naming a signal this build cannot compute has made a claim that cannot
be checked, and treating an unimplemented check as a pass is the failure mode the
whole phase exists to avoid.
"""

from __future__ import annotations

from wcc.models.action import ActionObservation
from wcc.models.capability import ExpectedEffect
from wcc.models.execution import Verification
from wcc.models.session import PageSnapshot

URL_CHANGE = "url_change"
TITLE_CHANGE = "title_change"
DOCUMENT_CHANGE = "document_change"
RESULTS_COLLECTION_CHANGE = "results_collection_change"
COLLECTION_COUNT_CHANGE = "collection_item_count_change"
ELEMENT_APPEARED = "element_appeared"
ELEMENT_DISAPPEARED = "element_disappeared"
LOADING_SETTLED = "loading_settled"

#: Every signal this build can compute. Specification 11.3 also lists a control
#: value and a selected or checked state reaching an expected value; those are
#: verified per target rather than per page, and no capability the deterministic
#: compiler generates declares one, so a template naming them would be checked
#: against an expectation nothing produced. They are absent rather than stubbed.
COMPUTABLE_SIGNALS = frozenset(
    {
        URL_CHANGE,
        TITLE_CHANGE,
        DOCUMENT_CHANGE,
        RESULTS_COLLECTION_CHANGE,
        COLLECTION_COUNT_CHANGE,
        ELEMENT_APPEARED,
        ELEMENT_DISAPPEARED,
        LOADING_SETTLED,
    }
)


def _element_keys(snapshot: PageSnapshot) -> set[str]:
    """Identify elements across two snapshots without using their IDs.

    Element IDs are minted per observation, so every element of the second
    snapshot has a new one and a comparison by ID would report the whole page as
    having been replaced. The primary locator candidate is what the extractor
    thought identified the element, which is the same question asked here.
    """
    return {
        f"{element.locator_chain.primary.kind}:{element.locator_chain.primary.key}"
        for element in snapshot.elements
    }


def _collection_hashes(snapshot: PageSnapshot) -> list[str]:
    return sorted(collection.collection_hash for collection in snapshot.collections)


def _record_counts(snapshot: PageSnapshot) -> int:
    """How many records the page held, across every collection.

    ``record_count`` rather than ``len(records)``: a truncated collection keeps the
    count the page actually had, and comparing what survived our own display limit
    would read a limit as a result set that shrank.
    """
    return sum(collection.record_count for collection in snapshot.collections)


def observed_signals(
    before: PageSnapshot,
    after: PageSnapshot,
    observations: list[ActionObservation],
) -> list[str]:
    """Every signal of specification 11.3 that the two snapshots show."""
    seen: list[str] = []

    if before.url != after.url:
        seen.append(URL_CHANGE)
    if before.title != after.title:
        seen.append(TITLE_CHANGE)
    if before.document_id != after.document_id:
        seen.append(DOCUMENT_CHANGE)
    if _collection_hashes(before) != _collection_hashes(after):
        seen.append(RESULTS_COLLECTION_CHANGE)
    if _record_counts(before) != _record_counts(after):
        seen.append(COLLECTION_COUNT_CHANGE)

    before_keys, after_keys = _element_keys(before), _element_keys(after)
    if after_keys - before_keys:
        seen.append(ELEMENT_APPEARED)
    if before_keys - after_keys:
        seen.append(ELEMENT_DISAPPEARED)

    # The last word, not any word: an earlier step settling says nothing about the
    # page that the final step left behind.
    if observations and observations[-1].settled:
        seen.append(LOADING_SETTLED)

    return seen


def verify(
    expected: ExpectedEffect,
    before: PageSnapshot,
    after: PageSnapshot,
    observations: list[ActionObservation],
) -> tuple[Verification, list[str]]:
    """Judge one execution, returning the verdict and everything observed.

    An effect counts as observed when **at least one** of its declared signals is.
    :class:`~wcc.models.capability.ExpectedEffect` names one effect and then lists
    the ways it can show — a search shows as a URL change, a result-set change, or
    both, depending on whether the site pages by query string or by script.
    Requiring all of them would make the verdict depend on how the site is built
    rather than on whether the search happened.

    Requiring none of them would be worse, and is what the count is for: the ratio
    is reported so that a capability quietly matching one of two signals every time
    is visible without re-deriving it.
    """
    seen = observed_signals(before, after, observations)
    matched = [signal for signal in expected.signals if signal in seen]
    missing = [signal for signal in expected.signals if signal not in seen]
    return (
        Verification(
            passed=bool(matched),
            signals_expected=len(expected.signals),
            signals_matched=len(matched),
            observed=seen,
            missing=missing,
        ),
        seen,
    )
