# What it finds, what it refuses, and what one observation costs

Three measurements, in the order they matter. A cheaper observation of nothing is
worth nothing, so coverage comes first.

## Does it find anything on pages nobody here wrote?

Twenty-two public pages, one visit each, `robots.txt` fetched and honoured per
host. Observed **2026-10-22**, in a single run.

```bash
uv run python -m benchmarks.measure_coverage
```

| | |
|---|---|
| pages visited | 22 |
| skipped by `robots.txt` | 3 |
| failed to load | 1 (`www.gnu.org`, timeout) |
| **observed** | **18** |
| had something a compiler could want | 17 |
| **…and a capability was compiled** | **14 (82%)** |
| state-changing candidates refused | **21** |

The capabilities are real ones on real sites: search on `python.org`, `pypi.org`,
`crates.io`, `djangoproject.com` and `openstreetmap.org`; result-opening on
`books.toscrape.com`, `quotes.toscrape.com`, `arxiv.org`, `w3.org`, `debian.org`,
`go.dev`, `docs.python.org` and `developer.mozilla.org`; next-page on
`news.ycombinator.com`.

**The 21 refusals matter as much as the 14 successes.** Twenty of them are the
"add to basket" forms on `books.toscrape.com` and one is a subscription form on
`debian.org`. A low-level agent sees those as buttons and can press them. WCC
classified them as state-changing, reported them, and did not offer them. That is
the actual product; the token figures below are a side effect of it.

### What changed since 2026-10-13

Opening a page now waits for it to stop changing before the first observation
([`progress.md`](progress.md), 2026-10-22). Before that, the first observation was
taken at the load event, and a client-rendered page had not drawn anything yet.

| Page | 2026-10-13 | 2026-10-22 | Reading |
|---|---|---|---|
| `crates.io` | 0 elements, nothing | 89 elements, search, open a result | the fix; "open a result" is wrong, see below |
| `www.djangoproject.com` | search | search, open a result | "open a result" now binds, and it is wrong, see below |

Element counts moved by one to five on six other pages; no other capability changed.
The run also now records templates that compiled but did not bind, which the
October 13 file did not, so the bindings below cannot be compared with it:

| Page | Compiled, not bound | Why |
|---|---|---|
| `huggingface.co` | `open_result_item`, `navigate_to_page` | binding re-classified the risk on the live page and it rose from low to high |
| `docs.python.org` | both search forms | the same |
| `www.openstreetmap.org` | `open_result_item` | the same |
| `www.rust-lang.org` | `open_result_item` | its container no longer resolved exactly once |

### What changed from 2026-08-11 to 2026-10-13

The August run observed 17 pages and compiled something on 11. "Open a result"
now comes from a detected collection rather than from the largest group of
same-path links ([`progress.md`](progress.md), 2026-10-13), and that moved four
pages:

| Page | August | October | Reading |
|---|---|---|---|
| `quotes.toscrape.com` | nothing | open a quote | the miss below, fixed |
| `docs.python.org` | nothing | open a section of the index | fixed by the same change |
| `developer.mozilla.org` | nothing | open a recent contribution | the home page's list of ten |
| `www.djangoproject.com` | search, open a result | search | the only list outside navigation is the footer; its binding is refused |

`crates.io` went from search and open-a-result to nothing because the page
yielded **zero** elements that time, against 29 in August. It is client-rendered
and the observation, not the compiler, came back empty: it was read before the
page had rendered. Fixed on 2026-10-22, above.

### Compiled, and wrong

| Page | What "open a result" points at |
|---|---|
| `pypi.org` | three footer links (`trademarks`, …). The footer is a `div`, not a `<footer>`, so it is not excluded as navigation, and the home page has no result list to beat it |
| `news.ycombinator.com` | the thirty user names. A story is three table rows and records are detected one row at a time; the title row's first link is the upvote arrow, so taking it would be worse |
| `crates.io` | three footer links, one from each footer column (`rust-lang.org`, `The Cargo Book`, `Usage Policy`). The footer columns are detected as cards with a heading beyond their link, and cards that say something beyond their link are preferred to the page's real lists of ten crates, which are bare links |
| `www.djangoproject.com` | five footer links, one from each footer column (`About Django`, `Join a Group`, …), for the same reason |

None of these changes the page count — every one of these pages also has a correct capability — but
an agent offered these would open the wrong thing. They are kept visible here
rather than filtered by a rule that would only move the mistake: on `pypi.org` the
next candidate is two paragraphs of introduction.

### Where it found nothing

Separated honestly, because a zero on `example.com` and a zero on a search page
are not the same event.

| Page | Reading |
|---|---|
| `example.com` | correct — one paragraph and one link |
| `www.rust-lang.org` | probably correct — no forms; "open a result" compiled and did not bind |
| `webscraper.io` | **not investigated** — 41 elements, no forms, nothing compiled |
| `huggingface.co` | **not yet explained** — 133 elements; "open a result" and "go to page" compiled, and binding refused both because their risk rose from low to high on the live page |

# What one observation costs

**Status: partial.** This page reports a measurement that was made. It is not the
benchmark specification 14 describes, and the difference is stated here rather
than in a footnote, because the missing half is the half most people will assume
is present.

## What was measured

For each fixture page, the payload returned by one low-level `browser_snapshot`
against the payload returned by one WCC `inspect_page`. Same page, same browser,
same moment.

| Page | Size | Baseline snapshot | WCC inspection | WCC / baseline |
|---|---|---:|---:|---:|
| `search-page` | 6 controls | 169 | 464 | 2.75× |
| `filter-page` | 5 controls | 78 | 338 | 4.33× |
| `product-list` | 3 results | 149 | 622 | 4.17× |
| `large-catalog` | 150 products | 5,187 | **700** | **0.13×** |

Tokens, counted with `o200k_base` — OpenAI's published tokenizer, run offline.
**Not Claude's tokenizer**, and the difference is stated wherever these numbers
appear. Anthropic does not publish one for current models, so an exact Claude
count needs an API key; what a consistent ruler does give correctly is the *ratio*
between two payloads of similar text, which is what the claim rests on.

Reproduce with:

```bash
uv run python -m benchmarks.measure_payloads
```

No API key, no cost, no inference, no network beyond a loopback fixture server and
one cached tokenizer download.

The command uses the most authoritative counter it can reach and says which one
answered: Anthropic's `count_tokens` when `ANTHROPIC_API_KEY` is set (exact for
Claude, free, no inference), `tiktoken` otherwise, and characters if neither is
available. The unit travels with the numbers — into the table, into
`benchmarks/results/payload-sizes.json`, and into the code — so a figure cannot be
quoted later as something it is not.

## Pages nobody here wrote

Every fixture above was written by the person claiming the result, so the same
measurement was pointed at public pages. Observed **2026-08-11**; these are dated
supplements, not the benchmark of record, because a site can change between two
runs (specification 14.4).

| Site | Page | Baseline | WCC | WCC / baseline |
|---|---|---:|---:|---|
| `example.com` | the smallest real page there is | 49 | 164 | 3.35× |
| `quotes.toscrape.com` | a listing built for scraping practice | 888 | 166 | 0.19× |
| `books.toscrape.com` | a 20-item product grid | 1,637 | 610 | 0.37× |
| `www.python.org` | a large marketing and navigation page | 3,150 | 722 | 0.23× |
| `news.ycombinator.com` | 30 links, minimal markup | 3,630 | 870 | 0.24× |
| `developer.mozilla.org` | a long documentation page | 6,125 | **449** | **0.07×** |

```bash
uv run python -m benchmarks.measure_public
```

One page per site, one visit each, read-only, no crawling, nothing behind a login.
`robots.txt` was checked for all six.

**This is where the fixtures were caught being too kind.** On the first run WCC
*lost* on Hacker News (1.25×) and on the books listing (1.14×) — and the reason
was not the design but two defects that a hand-written fixture could not produce.
Hacker News emitted sixty-four copies of one warning, each naming a different
internal record ID, together larger than every capability on the page; and its
layout tables put a whole navigation bar into a single cell, which arrived as an
entity property at full length. Warnings are now deduplicated with a count and
sampled properties are bounded. The same pages then came back at 0.24× and 0.37×.

Real sites found both in one afternoon. Neither would ever have appeared in a
fixture, because nobody writes a fixture that emits the same warning sixty-four
times.

## What it shows

The two costs are proportional to different things, and that is the whole result.

A snapshot carries everything on the page, so it grows with the page. An
inspection carries the capabilities the page offers, so it grows with the
controls. The 150-product catalog has roughly the same handful of controls as the
small search page and thirty times the content — and the ratio crosses over
between them.

Stated as precisely as nine pages allow:

> **An inspection has a fixed cost, so on a nearly empty page it is more expensive
> than a snapshot. As a page's content grows without its number of distinct
> operations growing, the two costs cross over and the gap widens.**

That is a claim about a break-even point, not about winning. It is deliberately
weaker than "WCC is cheaper on real pages", which nine observations cannot
support: these are six sites at one page each, chosen by the author, on one day.
What they do show is that the crossover happens early — every page here with more
than a paragraph of content is past it.

The mechanism is the part worth remembering: **a snapshot's cost is set by how
much is on a page; an inspection's is set by how much you can *do* on it.** Most
pages contain far more than they offer, which is why the crossover sits where it
does. A page that inverts that — many distinct controls, little content — should
favour the snapshot, and none of the nine pages here is that shape. That is a gap
in the sample, not evidence of absence.

## What the losing pages are made of

Both losses were opened up rather than explained away, because "we lose here and
here is why" is a different sentence from "we lose here".

**`example.com` — 164 tokens, of which the capabilities are `[]`.** The payload is
`page_signature` (73 characters), three identifiers (92), and a URL. WCC spends
164 tokens to say there is nothing here you can do. That is fixed cost, it does
not shrink with the page, and it is the entire loss.

It was left alone. The obvious way to improve the number is to stop returning
`page_signature` — but specification 12.2 names it in the output, and shaving a
field that the contract requires in order to look better in a measurement is the
exact move this project spent the day catching in itself.

**`search-page` — 1,787 characters, of which 740 are capabilities and 606 were
refusals.** The 740 is the product: argument schemas, risk, confidence, things a
snapshot does not provide at all. The 606 was not. It was two prose sentences
explaining *why* a form had not been compiled — written for a person reading a
test failure, and sent to an agent on every inspection, where the only decision
they inform is "there is a form here I cannot offer you".

Those are now one clause each. The saving is small (484 → 464 tokens on this page)
and it is the right change independent of any measurement: the rule belongs in the
documentation, the response carries the fact.

An external review argued this point and was right. It also argued for dropping
`page_signature`, without checking that specification 12.2 requires it.

## What it does not show

**How many observations a task takes.** This is the important omission. WCC's
saving is supposed to come mostly from *asking fewer times*: a baseline agent's
element refs expire with the snapshot that produced them, so it snapshots again
before each interaction, while an inspection binds capabilities that stay valid.
That difference cannot be measured without running an agent, and no agent was run.

So the claim supported here is "one observation is cheaper on a realistic page",
not "a task costs less". The second is the interesting one and it is not yet
evidenced.

**Claude's own token counts.** The table is `o200k_base`, not Claude. An exact
Claude count needs an API key, and the run that produced these numbers had none.
Nothing here is converted or scaled to compensate: re-running with a key replaces
the unit rather than adjusting it.

For what it is worth, the two rulers agree about the conclusion. The same
measurement in characters gave 3.17× and 0.17× where tokens give 2.80× and 0.14×
— within about 15%, across a difference of a factor of twenty. The finding never
depended on the precision, which is the main reason it was safe to publish before
having the exact numbers.

## What would complete it

`benchmarks/run.py` is written and tested: three arms (a Playwright-MCP-style
baseline, WCC cold, WCC warm), one shared agent loop, at least three runs per
condition, success decided by the fixture rather than by the model, and raw runs
written to `benchmarks/results/`. It needs an API key and roughly $0.16.

```bash
uv run python -m benchmarks.run --dry-run   # builds everything, calls nothing
uv run python -m benchmarks.run             # needs ANTHROPIC_API_KEY
```

Until that has been run, this page is what there is, and it says so.

## A note on how the number above was found

The first version of this measurement said WCC cost **2.8× more** than the
baseline on the large catalog — the opposite of the project's claim, with all 553
tests passing.

The cause was one word. Specification 12.2 says `inspect_page` returns an entity
*summary*; the implementation returned every entity, so a 150-product page
produced a 42,239-character inspection. Returning a summary — a total, counts by
type, five examples — is what produces the 2,527 above.

It was found by measuring before paying for anything, and by believing the first
measurement instead of assuming the harness was wrong. Both are recorded here
because a benchmark's credibility rests on what its author did when the number
came out unflattering.

There is now a test asserting that a WCC inspection of the large catalog costs
less than half a baseline snapshot. A project whose claim is "cheaper" should have
the cheapness under test; functional tests fail when the function breaks, not when
the argument does.
