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
| `search-page` | 6 controls | 169 | 484 | 2.86× |
| `filter-page` | 5 controls | 78 | 350 | 4.49× |
| `product-list` | 3 results | 149 | 623 | 4.18× |
| `large-catalog` | 150 products | 5,187 | **705** | **0.14×** |

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
| `quotes.toscrape.com` | a listing built for scraping practice | 888 | 167 | 0.19× |
| `books.toscrape.com` | a 20-item product grid | 1,637 | 613 | 0.37× |
| `www.python.org` | a large marketing and navigation page | 3,150 | 706 | 0.22× |
| `news.ycombinator.com` | 30 links, minimal markup | 3,610 | 874 | 0.24× |
| `developer.mozilla.org` | a long documentation page | 6,125 | **438** | **0.07×** |

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

**WCC is more expensive on trivial pages and several times cheaper on realistic
ones.** It loses on `example.com` and on the small fixtures by a few hundred
tokens, because the fixed cost of a capability schema is larger than a snapshot of
a page with almost nothing on it. It wins on every real page with actual content,
by between 4× and 14×.

The clean way to state it: **a snapshot's cost is set by how much is on the page;
an inspection's is set by how much you can *do* on it.** Most real pages contain
far more than they offer.

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
