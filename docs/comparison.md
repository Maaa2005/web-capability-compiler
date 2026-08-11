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
| `search-page` | 6 controls | 563 | 1,787 | 3.17× |
| `filter-page` | 5 controls | 253 | 1,142 | 4.51× |
| `product-list` | 3 results | 444 | 2,241 | 5.05× |
| `large-catalog` | 150 products | 15,234 | 2,527 | **0.17×** |

Characters, counted exactly. Reproduce with:

```bash
uv run python -m benchmarks.measure_payloads
```

No API key, no cost, no network beyond a loopback fixture server.

## What it shows

The two costs are proportional to different things, and that is the whole result.

A snapshot carries everything on the page, so it grows with the page. An
inspection carries the capabilities the page offers, so it grows with the
controls. The 150-product catalog has roughly the same handful of controls as the
small search page and thirty times the content — and the ratio crosses over
between them.

**WCC is more expensive on trivial pages and about six times cheaper on realistic
ones.** On the pages where it loses, it loses by fractions of a cent; the fixed
cost of a capability schema is simply larger than a snapshot of six controls.

## What it does not show

**How many observations a task takes.** This is the important omission. WCC's
saving is supposed to come mostly from *asking fewer times*: a baseline agent's
element refs expire with the snapshot that produced them, so it snapshots again
before each interaction, while an inspection binds capabilities that stay valid.
That difference cannot be measured without running an agent, and no agent was run.

So the claim supported here is "one observation is cheaper on a realistic page",
not "a task costs less". The second is the interesting one and it is not yet
evidenced.

**Tokens.** The numbers above are characters. Specification 14.3 requires token
counts to come from provider usage metadata rather than from an estimate, so no
token figure and no percentage is published here. Four characters per token is a
common approximation and it is not a measurement.

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
