"""The held-out plan's numbers, frozen before the sample was drawn.

``PLAN.md`` explains each one. Code reads them from here so the plan cannot say
one thing while the harness does another. Changing anything in this file after
the run has been scored invalidates the run; changing it before must be listed
in ``PLAN.md``'s change log with the date.
"""

from __future__ import annotations

from dataclasses import dataclass

# -- the sampling frame ------------------------------------------------------

#: The Chrome UX Report top-million list, as cached by
#: https://github.com/zakird/crux-top-lists. The September 2026 file was the
#: newest when the plan was written.
FRAME_REPOSITORY = "https://github.com/zakird/crux-top-lists"
FRAME_COMMIT = "35ba243941defcd21882b134d9dd354824922482"
FRAME_PATH = "data/global/202609.csv.gz"
FRAME_SHA256 = "be444a341be6b4b0ffb2acae08acef67c77243c01bb071a69b26f87c53011f88"

#: The shuffle's seed: the first eight hex digits of the frame file's SHA-256.
#: Taken from data nobody here controls, so it could not be chosen by trying
#: seeds until a sample looked convenient.
SEED = int(FRAME_SHA256[:8], 16)


@dataclass(frozen=True)
class Stratum:
    name: str
    ranks: tuple[int, ...]
    """CrUX rank buckets in this stratum. The file has seven: 1,000, 5,000,
    10,000, 50,000, 100,000, 500,000 and 1,000,000."""


STRATA: tuple[Stratum, ...] = (
    Stratum("top-1k", (1_000,)),
    Stratum("1k-10k", (5_000, 10_000)),
    Stratum("10k-100k", (50_000, 100_000)),
    Stratum("100k-1m", (500_000, 1_000_000)),
)

#: Pages scored per stratum, and how many candidates are drawn in order to reach
#: that many. Candidates are used strictly in the drawn order.
PAGES_PER_STRATUM = 15
CANDIDATES_PER_STRATUM = 60

# -- exclusions decided before drawing ----------------------------------------

#: Registrable domains of the 22 development pages in
#: ``benchmarks/measure_coverage.py`` (and the six of ``measure_public.py``,
#: all among them). Their rules were tuned on these sites, so no origin on one of
#: these domains or a subdomain of it may be drawn.
DEVELOPMENT_DOMAINS: tuple[str, ...] = (
    "arxiv.org",
    "crates.io",
    "debian.org",
    "djangoproject.com",
    "example.com",
    "gnu.org",
    "go.dev",
    "huggingface.co",
    "lobste.rs",
    "mozilla.org",
    "openstreetmap.org",
    "palletsprojects.com",
    "pypi.org",
    "python.org",
    "rust-lang.org",
    "toscrape.com",
    "w3.org",
    "webscraper.io",
    "wikipedia.org",
    "ycombinator.com",
)

#: Host-name words that mark adult sites, filtered before any visit so nobody is
#: asked to label one. Crude on purpose and fixed in advance; the annotator
#: excludes any that get through.
ADULT_HOST_WORDS: tuple[str, ...] = (
    "porn",
    "xxx",
    "sex",
    "hentai",
    "xvideo",
    "xnxx",
    "xhamster",
    "nsfw",
    "escort",
    "nude",
    "fetish",
    "milf",
    "erotic",
    "onlyfans",
    "chaturbate",
    "stripchat",
)

# -- pass thresholds ---------------------------------------------------------

#: Point estimates over the scored pages. Wilson 95% intervals are reported
#: beside them and do not change the verdict.
SEARCH_MIN_PRECISION = 0.90
SEARCH_MIN_RECALL = 0.60
RESULTS_MIN_PRECISION = 0.70
RESULTS_MIN_RECALL = 0.40
MAX_UNSAFE_EXPOSURES = 0

#: T1 counts capabilities WCC would run. Above ``low`` it refuses to run them
#: (specification 10.1). A capability with any other value, or none, counts.
NON_EXECUTABLE_RISKS: tuple[str, ...] = ("medium", "high", "critical")

#: A precision or recall computed from fewer cases than this is reported but not
#: judged, and the overall verdict is then "not judged" rather than a pass.
MIN_CASES_TO_JUDGE = 10

#: How a labelled result title is matched to WCC's result list: equal after
#: normalisation, or the shorter is at least this long and inside the longer.
TITLE_CONTAINMENT_MIN_CHARS = 10

#: Pages in the optional agreement check, drawn with SEED from the scored pages.
AGREEMENT_PAGES = 15

# -- what counts as WCC offering each type -------------------------------------

#: Q1 is answered by a search capability, or by a filter capability with a
#: free-text argument: either lets an agent search the site by words.
SEARCH_CATEGORY = "search"
FILTER_CATEGORY = "filter"
RESULT_LIST_NAME = "open_result_item"
#: Q3 is answered by either way WCC compiles "go to the next page".
NEXT_PAGE_NAMES: tuple[str, ...] = ("navigate_next_page", "navigate_to_page")
