# What it finds, what it refuses, and what it costs

Four measurements, in the order they matter. A cheaper observation of nothing is
worth nothing, so coverage comes first.

## Does it find anything on pages nobody here wrote?

Twenty-two public pages, one visit each, `robots.txt` fetched and honoured per
host. Observed **2026-10-22**, in a single run after the footer-column fix below
(`benchmarks/results/coverage-20261022-footer.json`).

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
`books.toscrape.com` (20 books), `quotes.toscrape.com` (10 quotes),
`webscraper.io` (3 products), `w3.org` (4 news items), `debian.org` (5 news items)
and `djangoproject.com` (2 news posts); next-page on `news.ycombinator.com`.
Result-opening on `arxiv.org` and `docs.python.org` points at the right kind of
link but only part of it: arxiv's 11 physics archives without mathematics,
computer science and the rest, and the 6 index sections of one column of
docs.python.org.

Not all fourteen pages are carried by a correct capability. `go.dev`'s only
capability is wrong, so the 14 includes one page that compiled something an agent
should not use; see [Compiled, and wrong](#compiled-and-wrong).

**The 21 refusals matter as much as the 14 successes.** Twenty of them are the
"add to basket" forms on `books.toscrape.com` and one is a subscription form on
`debian.org`. A low-level agent sees those as buttons and can press them. WCC
classified them as state-changing, reported them, and did not offer them. That is
the actual product; the token figures below are a side effect of it.

### What changed since 2026-10-13

Opening a page now waits for it to stop changing before the first observation
([`progress.md`](progress.md), 2026-10-22). Before that, the first observation was
taken at the load event, and a client-rendered page had not drawn anything yet.

| Page | 2026-10-13 | 2026-10-22, settle fix | Reading |
|---|---|---|---|
| `crates.io` | 0 elements, nothing | 89 elements, search, open a result | the fix; "open a result" was footer links, removed by the footer fix below |
| `www.djangoproject.com` | search | search, open a result | "open a result" now bound, to footer links; the footer fix moved it to the news posts |

Element counts moved by one to five on six other pages; no other capability changed.

Within the same day, "open a result" stopped treating a row of footer columns as a
result list ([`progress.md`](progress.md), "Footer columns are not a result
list"). The run before that fix (`coverage-20261022.json`) and the one after it
have the same totals and differ on four pages:

| Page | Before the footer fix | After |
|---|---|---|
| `crates.io` | search, open a result (three footer links) | search |
| `www.djangoproject.com` | search, open a result (five footer links) | search, open a result (the two latest news posts) |
| `developer.mozilla.org` | open a result (footer links) | nothing |
| `webscraper.io` | nothing | open a result (the three products) |

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
| `developer.mozilla.org` | nothing | open a result | the target was not recorded; on 2026-10-22 the same capability pointed at footer links, and the footer fix removed it |
| `www.djangoproject.com` | search, open a result | search | "open a result" pointed at the footer and its binding was refused |

`crates.io` went from search and open-a-result to nothing because the page
yielded **zero** elements that time, against 29 in August. It is client-rendered
and the observation, not the compiler, came back empty: it was read before the
page had rendered. Fixed on 2026-10-22, above.

### Compiled, and wrong

| Page | What "open a result" points at |
|---|---|
| `go.dev` | the first package of each use-case column (`cloud.google.com/go`, `spf13/cobra`, `net/http`, …) plus a "More use cases" card. Each column is a heading over a list of packages, like a footer, but the extra card heads no list, so the footer rule does not remove the row |
| `www.python.org` | the first project of each "Use Python for…" row (`Django`, `tkInter`, `PyTorch`, …). Each row is a run of sibling links rather than a list the collection detector reports, so nothing marks them as the heads of lists |
| `pypi.org` | three footer links (`trademarks`, …). The footer is a `div`, not a `<footer>`, so it is not excluded as navigation, and its links sit in sentences, so no list of their own marks them |
| `news.ycombinator.com` | the thirty user names. A story is three table rows and records are detected one row at a time; the title row's first link is the upvote arrow, so taking it would be worse |

`go.dev`'s count depends on this: it is the page's only capability, so one of the
fourteen pages is counted on a wrong capability. The other three also have a
correct one — search on python.org and pypi, next-page on Hacker News — and an
agent offered these would still open the wrong thing. They are kept visible here
rather than filtered by a rule that would only move the mistake: on `pypi.org` the
next candidate is two paragraphs of introduction.

Two more compiled a wrong target that binding refused, so it was never offered:
`www.openstreetmap.org` (the `Fastly` and `Terms of Use` links) and
`huggingface.co` (the task filters of its sidebar, `Text Generation`, `PyTorch`, …).

### Where it found nothing

Separated honestly, because a zero on `example.com` and a zero on a search page
are not the same event.

| Page | Reading |
|---|---|
| `example.com` | correct — one paragraph and one link |
| `www.rust-lang.org` | probably correct — no forms; "open a result" compiled over four "Building Tools, Writing Web Apps, …" cards and its container did not resolve exactly once at binding |
| `developer.mozilla.org` | a miss — 378 elements, no forms; the only "open a result" it ever compiled was the footer; why nothing else on the page qualifies is not yet investigated |
| `huggingface.co` | correct refusal of a wrong target — 133 elements; "open a result" compiled over the sidebar's filters and "go to page" beside it, and binding refused both because their risk rose from low to high on the live page |

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

**What a whole task costs.** This table measures one observation. A task is
measured separately, by running an agent, [below](#what-a-task-costs).

This page used to say WCC's saving would come mostly from *asking fewer times*:
that a baseline agent's element refs expire with the snapshot that produced them,
so it snapshots again before each interaction. The agent runs showed that was
mostly wrong. On both search tasks every baseline run took exactly two snapshots,
one at the start and one at the end; it typed and clicked on the refs of the
first. The large-page cost came from one large snapshot being read again as
history on every later request, not from taking many. Repeated snapshots happened
only on the filter task.

**Claude's own token counts.** The table is `o200k_base`, not Claude. An exact
Claude count for one payload needs an API key, and the run that produced these
numbers had none. Nothing here is converted or scaled to compensate: re-running
with a key replaces the unit rather than adjusting it. The agent runs below do use
Claude's own counts, but they measure whole tasks, so they do not replace this
table.

For what it is worth, the two rulers agree about the conclusion. The same
measurement in characters gave 3.17× and 0.17× where tokens give 2.80× and 0.14×
— within about 15%, across a difference of a factor of twenty. The finding never
depended on the precision, which is the main reason it was safe to publish before
having the exact numbers.

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

# What a task costs

The agent benchmark of specification 14: three tasks on local fixtures, three
arms, three attempts each. Run once on 2026-10-22, with the harness frozen at
commit `2aa9d7a`. Every run, request by request, is in
`benchmarks/results/runs-20261022T124255Z.json`.

```bash
uv run python -m benchmarks.run
```

One agent does each task with either a Playwright-MCP-style baseline's low-level
browser tools or WCC's six tools — cold, with nothing stored, and warm, with the
templates the cold run stored, on a different input. It runs as Claude Code
signed in to a subscription: no API key, nothing billed per token, and the counts
are the provider's usage for every request, not an estimate. The model was
requested as `claude-haiku-4-5`, and every run was answered by
`claude-haiku-4-5-20251001`.

All 27 runs met their verification condition — the task's input reached the
results page's address and its visible text — so none is missing from the
medians. No run executed anything above `low` risk. All nine warm runs bound a
stored template on their first inspection.

Total tokens, input plus output, median and observed range over three runs:

| Task | baseline | WCC cold | WCC warm |
|---|---:|---:|---:|
| search, `search-page` (6 controls) | 12,052 (9,739–12,056) | 10,191 (10,187–10,229) | 9,859 (9,783–9,922) |
| search, `large-catalog` (150 products) | 44,453 (27,204–44,558) | 10,716 (10,671–10,722) | 10,690 (10,617–15,910) |
| filter, `filter-page` | 25,687 (13,986–50,643) | 9,838 (9,835–9,899) | 9,896 (9,864–13,287) |

Median tool calls and seconds:

| Task | baseline | WCC cold | WCC warm |
|---|---:|---:|---:|
| search, 6 controls | 5 calls, 12.3 s | 3 calls, 11.7 s | 3 calls, 11.7 s |
| search, 150 products | 5 calls, 12.5 s | 3 calls, 12.7 s | 3 calls, 12.2 s |
| filter | 10 calls, 16.2 s | 3 calls, 11.8 s | 3 calls, 11.6 s |

No percentage is given. The arms are reported independently (specification
14.4), and one ratio over the three tasks would average the page where WCC does
not win with the pages where it does.

## What it shows

**On the six-control page, no clear win.** Two baseline runs cost 12,052 and
12,056 tokens. The third cost 9,739, less than any WCC run (the cheapest was
9,783). The ranges overlap. That is the break-even the observation table
predicts: on a page this small, an inspection is larger than a snapshot.

**On the 150-product page, the baseline reads one snapshot many times.** Its
first request read 1,399 input tokens. Then the snapshot — about 6,800 tokens of
catalog — rode along as history on every later request: 8,179, 8,355, 8,495,
8,641 and 8,939, for 44,008 input tokens over six requests. The attempt that
finished in four requests read 26,857. WCC's requests on the same page read
1,535, 1,799, 2,738 and 4,212.

**Most of that re-reading came from the prompt cache.** In that six-request run,
33,650 of the 44,008 input tokens were cache reads, which are billed at a
fraction of fresh input. So the gap is in tokens the model read, not in price,
and no price is computed here.

**On the filter task, the baseline varied and WCC did not.** The baseline's
three runs took 3, 5 and 8 snapshots (6, 10 and 16 tool calls) and cost 13,986,
25,687 and 50,643 tokens. Every WCC run took three or four calls. The filter
page is small, so this gap is mostly the per-request cost repeated: each baseline
request read 1,400–4,500 input tokens. It is also the only task where the
seconds differ.

**Warm cost the same as cold.** On every task the two medians are within 340
tokens and half a second of each other. Binding a stored template instead of
compiling saved nothing measurable here: the model reads an inspection of about
the same size either way, and on these fixtures the compile does not show in the
seconds. The two highest warm runs (15,910 and 13,287) are the two that looked at
the page a second time. On the large page it was an `inspect_page` after the
search, on the results page, which had nothing stored, so it compiled. On the
filter page it was a `list_capabilities` before executing, which inspects again,
and it bound.

## What it does not show

**Another route's numbers.** Claude Code adds about 540 input tokens to every
request (539–541, measured with `--calibrate`). It is the same for every arm,
but it is paid per request, so it widens every gap that comes from the number of
requests — the filter task's most of all. Through the API (`--provider api`) the
totals would be lower and those gaps narrower. These figures hold for this route,
this model and these fixtures.

**Public sites.** The benchmark of record is local fixtures, as specification
14.4 requires. No agent was run on a public page.

**Evidence coverage.** The Phase 7 reporting rules ask that capability evidence
coverage equal 100%. This run does not measure it. It is enforced by a Phase 3
acceptance test that fails if any template compiled from the search, filter,
product-list or pagination fixtures carries no evidence
(`tests/test_phase3_acceptance.py`). The large catalog is not in that test.
