"""What the agent is asked to do, and how the fixture decides whether it did it.

Specification 14.4: "a task must meet its verification condition to count as
completed". So a task carries two things that must not be confused — the wording
given to the model, and a check run against the browser afterwards that the model
has no way to influence.

The cold and warm variants of a task differ in their input value, per 14.2. That
is not cosmetic: reusing one keyword would let the second run answer from the
browser's cache or the site's, and the warm arm would be credited for a saving
that belongs to HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Task:
    """One thing to ask for, in two interchangeable wordings."""

    task_id: str
    """Stable, and used in filenames under ``benchmarks/results/``."""

    page: str
    """The fixture path the run starts on, relative to the fixture server."""

    cold_input: str
    warm_input: str
    """Different values for the two runs, per specification 14.2."""

    instruction: str
    """The wording given to the model. ``{input}`` is substituted.

    Identical between arms. Specification 14.2 permits differing tool
    descriptions and nothing else, so a prompt that told the WCC arm which
    capability to call would be measuring a hint rather than a tool surface.
    """

    expects_text: str
    """Text that must be present on the page afterwards for the run to count.

    Read from the live page rather than from the model's answer. A model that
    reports success it did not achieve is a failed run, and specification 14.4
    forbids merging that run into a token-reduction figure.
    """

    input_parameter: str | None = None
    """The query parameter the input value must have reached, or ``None`` for any.

    Every fixture results page is static — it shows the same records whatever was
    searched for — so the text alone says only that a results page was reached.
    The address is what records *which* search was made, and a run that searched
    for something other than it was asked to has not done the task.
    """

    def prompt(self, value: str) -> str:
        return self.instruction.format(input=value)

    def input_for(self, arm: str) -> str:
        return self.cold_input if arm in {"baseline", "wcc_cold"} else self.warm_input


#: The benchmark of record. Local fixtures, because specification 14.4 makes them
#: the reproducible arm and public sites only a dated supplement — a public site
#: can change between two runs and turn a measurement into an anecdote.
#:
#: Two of these are the same task at two page sizes, and that pairing is the
#: point. A first measurement showed that on a six-control fixture a WCC
#: inspection is *larger* than a baseline snapshot, which is true and says nothing
#: about real pages: WCC's saving comes from taking fewer observations, not
#: smaller ones, and a page with six controls has no observation cost to save.
#: Reporting one number from one page size would have answered a question nobody
#: asked. Reporting both measures what the saving actually depends on.
TASKS: tuple[Task, ...] = (
    Task(
        task_id="search_small_page",
        page="/search-page/",
        cold_input="wireless earbuds",
        warm_input="mechanical keyboard",
        instruction=(
            "Search this online shop for {input}. "
            "When the results are showing, reply with the word DONE and nothing else."
        ),
        expects_text="results",
        input_parameter="keyword",
    ),
    Task(
        task_id="search_large_page",
        page="/large-catalog/",
        cold_input="wireless earbuds",
        warm_input="mechanical keyboard",
        instruction=(
            "Search this online shop for {input}. "
            "When the results are showing, reply with the word DONE and nothing else."
        ),
        expects_text="results",
        input_parameter="keyword",
    ),
    Task(
        task_id="filter_results",
        page="/filter-page/",
        cold_input="audio",
        warm_input="wearables",
        instruction=(
            "Use this page's filter to show only products matching {input}. "
            "When the filter has been applied, reply with the word DONE and nothing else."
        ),
        expects_text="results",
        # The wording asks only for what the page offers: a keyword box and a
        # brand select, and no category control. An earlier wording asked for
        # "the {input} category", and in a smoke run before any run of record
        # the baseline spent all 25 turns looking for a control that is not
        # there — a measurement of the task's wording, not of either arm.
        input_parameter="keyword",
    ),
)
