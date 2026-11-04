# Held-out evaluation: plan

Written and committed **before any page in the sample was drawn or visited**.
The numbers live in [`plan.py`](plan.py) and the harness reads them from there.
Nothing in this file or in `plan.py` may change after the run is scored. A change
before that is allowed only with a dated entry in the change log at the end.

## The question

On a website nobody working on WCC has looked at, how often does WCC offer the
right capability, and how often does it offer a wrong one?

The published figure does not answer that. "13 of 17 pages produced a
capability (76%)" was measured on the 22 pages in
`benchmarks/measure_coverage.py`, and since August the compiler's rules have been
changed by looking at those same pages. They are development data. The
literature predicts a drop on new sites:

- Mind2Web's step success falls from 52.0 on held-out tasks to 38.9 on held-out
  websites (arXiv 2306.06070, Table 2).
- FreeDOM says hand-written wrappers "are usually brittle when testing on unseen
  websites" (arXiv 2010.10755, §6).

The 76% also counts any capability as success, so a wrong one counts. On 17
pages its 95% interval is about 53% to 90%.

## The sampling frame

- **List.** The Chrome UX Report top million, the September 2026 file
  (`data/global/202609.csv.gz` in github.com/zakird/crux-top-lists at commit
  `35ba2439`; SHA-256 in `plan.py`). The repository says CrUX is more accurate
  than the Alexa and Tranco lists, citing Ruth et al., IMC 2022. It ranks
  origins that Chrome users actually loaded. Tranco ranks domains, which include
  CDN and API names that serve no page.
- **Strata.** CrUX gives a rank bucket, not a rank. The file has seven buckets,
  grouped into four strata so that the long tail is not drowned out by the head:
  - top 1k (bucket 1,000)
  - 1k–10k (5,000 and 10,000)
  - 10k–100k (50,000 and 100,000)
  - 100k–1M (500,000 and 1,000,000)
- **Seed.** The first eight hex digits of the file's SHA-256, `0xbe444a34`. The
  seed comes from data nobody here chose, so it cannot have been picked by trying
  seeds until a sample looked easy.

## Drawing the sample

For each stratum, in this order:

1. Take its origins and sort them.
2. Shuffle them with Python's `random.Random(SEED)`. One generator is used for
   all four strata, in the order above.
3. Walk the shuffled order. Keep the first 60 origins that pass every rule
   below; these are that stratum's candidates, in order.

The rules, all fixed before drawing:

- **E1.** The scheme is `https`.
- **E2.** The host is not one of the 20 development domains in `plan.py`, or a
  subdomain of one.
- **E3.** The host contains none of the adult words in `plan.py`. Nobody should
  be asked to label such a site; the annotator excludes any that get through.
- **E4.** Its host differs from every origin already kept, in any stratum, once a
  leading `www.` or `m.` is removed. One site gets one page.

The drawn candidates are committed as `sample.json` before any visit.

## Visiting

The page is the origin's root, `https://host/`, followed through redirects as a
browser would follow them. For each candidate, in order:

1. **`robots.txt`.** Check it with the benchmark's `robots_refusal`. If it
   refuses, record the reason and move to the next candidate.
2. **Capture.** Open the page in plain Chromium. Wait for the load event plus
   up to 3 s of network quiet. Save a full-page screenshot (cut at 8,000 px)
   and the visible text. This is what the annotator will see.
3. **WCC.** Open the same URL with WCC:
   - the code as it is at the run's commit, recorded in the results;
   - a fresh store, so every page is compiled cold;
   - enrichment off and default limits, as in `measure_coverage.py`.

   Record:
   - every bound capability with its name and category;
   - the visible labels of the elements its evidence names;
   - the result list WCC took;
   - its refusals.

   Results go to a file the annotator does not open.
4. Pause 2 s, as `measure_coverage.py` does.

A stratum stops once 15 candidates have both a capture and a WCC result. A
candidate that failed either is recorded with the reason and replaced by the
next one in order. If labelling later excludes pages, the run continues down
the same order until each stratum has 15 scored pages. The report counts every
candidate used, so the share of the web that could not be measured is visible.

A stratum that uses up its 60 candidates before reaching 15 scored pages is
reported short. It is not topped up with a second draw.

## Labels

- **Who.** A person who has not seen WCC's output for these pages. The
  annotator's name and the time are recorded in `labels.json`.
- **When.** All labels are committed before the scoring step reads WCC's
  results.
- **From what.** The capture only: the screenshot and the visible text. Not the
  live site, which may have changed.

Questions, asked by `python -m benchmarks.heldout label` for each page:

| | Question | Answer |
|---|---|---|
| Q0 | Is this a page that can be labelled? Exclude it if it is adult, a login or age wall, a blank or error page, a parked domain, or not a web page. | yes, or no with one of those reasons |
| Q1 | Can a visitor type words to search this site? | yes / no |
| Q2 | Is there a main list of items (articles, products, posts, results), each opening its own page? | yes / no. If yes, type the first two titles as shown. |
| Q3 | Is there a link to the next page of such a list ("Next", "2", "More")? | yes / no |
| Q4 | Is there a control that would change something if used: sign up, log in, subscribe, add to cart, post, vote? | yes / no. If yes, type the visible text of up to three. |

**Agreement (optional).** If a second annotator is available, they label 15 of
the scored pages, drawn with the same seed, without seeing the first labels.
Cohen's κ is reported for Q1 to Q3. With one annotator, the report says
"single annotator" as a limitation. The second annotator's labels never replace
the first annotator's.

## What counts as WCC's answer

| Type | WCC offered it when |
|---|---|
| search | a bound capability has category `search` |
| result list | `open_result_item` is bound. Its labels are the result list WCC took. |
| next page | `navigate_to_page` is bound |

Matching to a label:

- **Search and next page.** By page: WCC offered it and the label says yes.
- **Result list.** By page, and only if both labelled titles are found among
  WCC's result-list labels. A title is found when the two strings are equal, or
  when the shorter is at least 10 characters long and contained in the longer.
  Both are compared after NFKC normalisation, case folding and collapsing
  whitespace. If WCC offered a list and the titles are not found, the list was
  the wrong one: it counts against precision, and against recall if the page
  has a list.
- **Unsafe exposure.** A page where a bound capability's evidence includes an
  element whose label equals, after the same normalisation, a control text from
  Q4. Every bound capability and its labels are printed in the report, so a
  reader can check this.

## Metrics

Over the scored pages, for search, result list and next page:

- **Precision.** Of the pages where WCC offered it, the share that were right.
- **Recall.** Of the pages labelled as having it, the share where WCC offered
  the right one.

Each comes with a Wilson 95% interval. Brown, Cai and DasGupta (2001) recommend
Wilson for small samples; that recommendation is quoted from secondary sources,
because the paper could not be fetched here.

Also reported:

- unsafe exposures, as a count and a Wilson upper bound;
- refused candidates;
- every result per stratum;
- the old measure, "pages with something that produced any capability". It is
  given twice: with the old proxy denominator (a form, or at least 20
  elements), and with the labels' denominator (Q1, Q2 or Q3 is yes). The first
  makes it comparable with the 76%.

## Pass line

The run **passes** only if all of these hold:

| | Criterion |
|---|---|
| T1 | unsafe exposures = 0 |
| T2 | search precision ≥ 0.90 and recall ≥ 0.60 |
| T3 | result-list precision ≥ 0.70 and recall ≥ 0.40 |

All are point estimates. A precision or recall with fewer than 10 cases behind
it is reported but not judged. The overall verdict is then **not judged**, not
a pass.

Why these numbers:

- **Precision is set high.** A wrong capability sends an agent somewhere it did
  not ask to go. This project already prefers offering nothing to offering
  something wrong (see "Compiled, and wrong" in `docs/comparison.md`).
- **Recall is set low.** WCC refuses rather than guesses: duplicate controls
  and tied lists compile nothing by design. Mind2Web's drop on unseen sites says
  recall will fall.
- **Next page has no threshold.** It is reported only, because a home page
  rarely has one and there will be too few positive cases to judge.

These lines were set by the agent that wrote this plan, before seeing any page
of the sample. The project owner may amend them **before** the run, with a
dated entry below; never after.

## After the run

- The run is reported whole, pass or fail: every page, label, result and
  exclusion.
- If WCC's rules are changed because of what this run showed, this sample is
  spent. It becomes development data. The next held-out run draws again with a
  new seed: the first eight hex digits of the SHA-256 of the next month's CrUX
  file, and the frame of that same month. It also excludes every domain in this
  sample.

## Known limitations, accepted in advance

- **Home pages only.** A result list or next page shows up less often there than
  on inner pages, which favours precision over recall for those types.
- **The `robots.txt` check can vary by Python version.** It uses the
  benchmark's reader, which is the standard library's, and the standard
  library's answer depends on the version. The Python version is recorded.
- **The capture and the WCC visit are seconds apart.** A site that changes its
  content on every load can fail the title match through no fault of WCC.
- **The CrUX list follows Chrome users.** It under-represents countries where
  Chrome is little used; its README says this about China.
- **A single annotator,** unless a second is found.

## Sources

The sources were read on 2026-11-04, through public mirrors where the original
sites were blocked here:

- **Mind2Web**, arXiv 2306.06070: §2.1, §4.1, Table 2.
- **WebLINX**, arXiv 2402.05930: Table 2, the out-of-domain splits.
- **FreeDOM**, arXiv 2010.10755, and **MarkupLM**, arXiv 2110.08518: the
  website as the unit held out, with page-level F1.
- **"An Illusion of Progress?"**, arXiv 2504.01382: small fixed benchmarks
  overstate progress.
- **Dwork et al.**, arXiv 1506.02629, and **Hofman et al.**, arXiv 2311.18807:
  a test set is used once.
- **Tranco**, arXiv 1806.01156, and **Scheitle et al.**, arXiv 1805.11506: state
  the list and its date.
- **crux-top-lists**, its README: the buckets and its accuracy claim.

## Change log

- 2026-11-04: plan written and committed before sampling.
- 2026-11-04, after drawing `sample.json` and before any visit: added the rule
  that a stratum which runs out of candidates is reported short rather than
  topped up. The plan had not said what happens then.
