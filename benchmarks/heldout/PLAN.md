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
- **E5.** Neither the host nor any parent domain of it is in a pinned public
  category list as adult or gambling (added 2026-11-04, before any visit; see the
  change log). The list is the University of Toulouse Capitole's UT1 blacklists,
  categories `adult`, `mixed_adult`, `gambling` and `arjel`. The draw reads
  `blocked_in_frame.txt`, the frame's hosts those files list.
- **E4.** Its registrable domain differs from that of every origin already kept, in
  any stratum, by the pinned Public Suffix List (`public_suffix_list.dat`, private
  section included, so `a.github.io` and `b.github.io` are two sites and
  `one.google.com` and `myactivity.google.com` are one). One site gets one page.
  Changed from a host comparison on 2026-11-07; see the change log.

The drawn candidates are committed as `sample.json` before any visit.

## Visiting

The page is the origin's root, `https://host/`, followed through redirects as a
browser would follow them. For each candidate, in order:

1. **`robots.txt`.** Read it through WCC's egress proxy, so the URL policy judges
   every redirect hop before anything connects to it, and answer by the rules of
   the benchmark's `robots_refusal`. If it refuses, or the proxy refuses a hop,
   record the reason and move to the next candidate. At most 512 KiB is read and
   parsed (RFC 9309 section 2.5), within 40 s for the whole read. A connection that
   fails, or a read that runs out of time, is no refusal: the candidate is recorded
   as unreachable, the run moves on, and a resumed run tries it again. So is a
   connection that fails at step 2 or 3. A redirect to a URL that cannot be parsed
   counts as a failed candidate.
2. **Capture.** Open the page through WCC's driver, behind the same proxy, and
   compile nothing. Wait for the load event plus up to 3 s for the page to go
   quiet. Save a full-page screenshot (cut at 8,000 px) and the visible text.
   This is what the annotator will see. The address the page ended on is then
   judged by E2 and E4, because the draw judged only the origin, and by the
   `robots.txt` of its own site, as is each address the browser was redirected
   through on the way there, because step 1 asked only about the origin's root. If
   any of these excludes it, the candidate is recorded as landed elsewhere and
   replaced, and its screenshot and text are not kept. The address WCC ended on at
   step 3 is judged the same way. For E4, "already kept" means the pages so far that
   have both a capture and a WCC result, by the address each started from and the
   addresses it ended on.
3. **WCC.** Open the same URL with WCC:
   - the code as it is at the run's commit, recorded in the results;
   - a fresh store, so every page is compiled cold;
   - enrichment off and default limits, as in `measure_coverage.py`.

   Record:
   - every bound capability with its name and category;
   - the visible text of the elements its evidence names, and beside it their
     accessible names, which can differ (`aria-label`) and which nothing is
     compared with;
   - the result list WCC took, the same two ways;
   - its refusals.

   An element WCC cannot find again has its name recorded in place of the visible
   text, and is counted. Results go to a file the annotator does not open.
4. Pause 2 s, as `measure_coverage.py` does.

A candidate that takes more than 180 s from step 1 to the end of step 3 is cut off,
recorded with the step it reached, and replaced.

A stratum stops once 15 candidates have both a capture and a WCC result. A
candidate that failed either is recorded with the reason and replaced by the
next one in order. If labelling later excludes pages, the run continues down
the same order until each stratum has 15 scored pages. The report counts every
candidate used, so the share of the web that could not be measured is visible.

A stratum that uses up its 60 candidates before reaching 15 scored pages is
reported short. It is not topped up with a second draw.

A stratum with fewer than 15 scored pages and candidates still unused, or still
waiting to be tried again after a connection failed, is unfinished, and is reported
as such.

A run does not start, or resume, unless `PLAN.md`, `plan.py` and `sample.json` are
committed as they stand, and a git command that fails stops it rather than reading as
"nothing changed".

A stopped run can be resumed only under the conditions it started with: the same
commit, code, plan and sample. `run.json` holds their fingerprints, and a mismatch
stops the run. A commit that touches nothing but the run's own directory, such as
the labels, is not a mismatch.

## Labels

- **Who.** A person who has not seen WCC's output for these pages. The
  annotator's name and the time are recorded in `labels.json`.
- **When.** All labels are committed before the scoring step reads WCC's
  results. The scoring step checks this first, and stops if a labels file, the plan
  or the sample is not committed as it stands, if git cannot say, if `run.json` is
  missing, or if the commit, code, plan or sample is not what `run.json` recorded,
  under the same rule as resuming. The report prints the plan's and the sample's
  SHA-256.
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
the scored pages, drawn with the same seed, without seeing the first labels. They
are a different person from the first annotator and start once every stratum is
complete or short and the first has finished, because the draw is kept from then on.
The pages are worked out without opening WCC's results: the capture index says whether
each WCC visit finished, and nothing else about it.
Cohen's κ is reported for Q1 to Q3. With one annotator, the report says
"single annotator" as a limitation. The second annotator's labels never replace
the first annotator's.

## What counts as WCC's answer

| Type | WCC offered it when |
|---|---|
| search | a bound capability has category `search`, or category `filter` with a free-text argument (a string without a fixed list of values) |
| result list | `open_result_item` is bound. Its labels are the result list WCC took. |
| next page | `navigate_next_page` or `navigate_to_page` is bound |

Matching to a label:

- **Search and next page.** By page: WCC offered it and the label says yes.
- **Result list.** By page, and only if both labelled titles are found among
  WCC's result-list labels. A title is found when the two strings are equal, or
  when the shorter is at least 10 characters long and contained in the longer.
  Both are compared after NFKC normalisation, case folding and collapsing
  whitespace. If WCC offered a list and the titles are not found, the list was
  the wrong one: it counts against precision, and against recall if the page
  has a list.
- **Unsafe exposure.** A page where the evidence of a bound capability WCC would
  run includes an element whose label equals, after the same normalisation, a
  control text from Q4. WCC refuses to run anything above risk `low`, so `medium`,
  `high` and `critical` are not exposures; any other value, or none, is treated as
  one. Every bound capability, its risk and its labels are printed in the report,
  so a reader can check this.

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
a pass. The same goes for a run with a stratum that is unfinished or short of 15
scored pages: it can fail, but it cannot pass.

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
- **The `robots.txt` check can vary by Python version.** It parses as the
  benchmark's reader does, with the standard library, and the standard library's
  answer depends on the version. The Python version is recorded.
- **A redirect is judged after the browser has followed it.** A page `robots.txt`
  refuses behind an allowed root is fetched once by the capture, because the browser
  follows the redirect before anything can ask. Nothing of it is kept, and WCC does
  not visit it.
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
- 2026-11-04, before any visit: the table of what counts as WCC's answer had
  two errors, found by a dry run of the harness on this project's own fixtures.
  No held-out page had been visited.
  - **Next page.** WCC compiles a `rel="next"` link as `navigate_next_page`, and
    numbered links as `navigate_to_page`. The table named only the second.
  - **Search.** A form WCC classifies as a filter but which has a free-text box
    lets an agent search by words, which is what Q1 asks. The table counted
    only category `search`.
- 2026-11-04, before any visit and before this sample was visited: **the project
  owner asked that adult and gambling sites be left out** (reported by the parent
  session: "成人向け・賭け事のサイトを除いて"). The host-word rule E3 catches only
  names with an obvious word, so rule **E5** was added.
  - **List.** UT1 (Université Toulouse Capitole), fetched from the university's
    own site, `https://dsi.ut-capitole.fr/blacklists/download/blacklists.tar.gz`
    (Last-Modified 2026-11-03 21:50:17 GMT, SHA-256 in `plan.py`). Licence CC BY-SA
    4.0, as stated in a GitHub mirror's README; the university's own page was
    unavailable (503) when read. Chosen over the Blocklist Project (licence stated
    two ways, repository archived), StevenBlack/hosts and OISD (much smaller, or no
    licence and no way to pin a version), and Cloudflare's categories (no bulk
    list). Precedent: RefinedWeb filters with a blocklist built on the same
    university's lists (arXiv 2306.01116, appendix G.1); *Beyond the Crawl*
    (arXiv 2502.01608) drops adult and gambling sites from a CrUX sample with
    Cloudflare's categories and says so under its limitations.
  - **Categories.** `adult` ("some adult site from erotic to hard pornography"),
    `mixed_adult` ("websites which contain adult sections unstructured"),
    `gambling` ("gambling and games sites, casino, etc") and `arjel` (the French
    gambling regulator's approved operators). Not used: `sexual_education`,
    which the list's own description says "can be misdetected as porn".
  - **Matching.** A host is out if it or any parent domain is listed. The lists
    carry no `www.`, so that is how `www.` hosts match.
  - **Pinning.** UT1 changes daily and publishes no hash, so the four category
    files are pinned by SHA-256 (`plan.py`), and their intersection with the
    frame is committed as `blocked_in_frame.txt` (15,342 hosts, SHA-256 in
    `plan.py`); the draw reads that file. Anyone with the same four files can
    rebuild it (`python -m benchmarks.heldout blocklist`).
  - **Limits, stated in advance.** A domain list marks a site, not a page, and its
    coverage is imperfect: Vallina et al. (IMC 2020) found category coverage
    "varies enormously" between providers and labels inconsistent. Some adult or
    gambling sites will remain, and the annotator still excludes any (Q0). Some
    sites listed will not be adult or gambling at all; they are out of the frame
    too, so **the claim of this run is about CrUX origins that UT1 does not mark
    adult or gambling, not about the web.** The sample is redrawn with the same
    seed and shuffle order, so a candidate survives unless E5 removes it.
  - **No change after the run.** This rule was fixed here, before any page was
    visited, and is not to be changed in the light of the result.
  - **CrUX terms.** The CrUX dataset by Google is under CC BY 4.0 (`developer.chrome.com/docs/crux/methodology`,
    "License"); the `crux-top-lists` repository has no licence file and calls
    itself "simply a cache of that public data". Attribution: CrUX (Google, CC BY
    4.0), September 2026 file. No restriction on publishing origin names was
    found; this is not legal advice. Google Cloud's dataset terms were not read.
- 2026-11-06, before any visit: a review of the harness found gaps, each
  reproduced on loopback. No held-out page had been visited.
  - **Capture.** Plain Chromium checked URLs only, so a WebSocket reached a refused
    address from a page and from a Worker. It now goes through WCC's driver and
    egress proxy.
  - **`robots.txt`.** The bare `urlopen` followed a redirect to a refused address
    before any safety check. Every hop now passes the proxy.
  - **Unfinished strata.** Ten good pages in one stratum and none in the others
    scored as a pass. Such a run is now not judged.
  - **Unsafe exposure.** It counted capabilities WCC refuses to run, which is
    stricter than "offered as something that can be run". It now counts only
    those WCC would run. The threshold stays at 0.
  - **Resuming.** A run resumed under a different commit, plan or sample without
    saying so. It now stops.
- 2026-11-06, before any visit: a second review of the harness found more, each
  reproduced on loopback. No held-out page had been visited. No threshold changed.
  - **Lost connections.** Every candidate after a dropped connection was recorded as
    refused by `robots.txt` and never tried again, and a name that did not resolve
    read as refused by the URL policy. They are now unreachable, and tried again on
    resume.
  - **Short strata.** Two strata of 15 and two of 0 scored as a pass, because a
    short stratum did not stop one. It now does. This is stricter than before.
  - **Scoring.** Nothing compared `run.json` with the present state or looked at
    whether the labels were committed, so an edited threshold changed the verdict
    silently. The scoring step now stops.
  - **Limits.** A `robots.txt` that never ended, and a page that hung the capture,
    each stopped the run. A bad redirect target on `robots.txt` crashed it. Each now
    costs one candidate at most.
  - **Visible text.** The harness recorded the name with `aria-label` first, while
    this plan compares what a person can see. It now records both and compares
    the visible text.
  - **Redirects.** E2 and E4 were judged on the origin only. The landing address is
    now judged too. E4 itself is unchanged and still compares hosts, so subdomains
    of one site can both be drawn; `sample.json` is unchanged.
  - **Second annotator.** The pages were drawn from everything the first annotator
    called eligible, not from the scored pages. A second annotator with the first's
    name is now refused.
- 2026-11-06, before any visit: a third review of the harness found more, each
  reproduced on loopback. No held-out page had been visited. No threshold changed, and
  `plan.py` and `sample.json` are unchanged.
  - **Committed first.** Nothing checked that the plan and the sample were committed
    when a run started, so an edited plan could be measured and, with only the labels
    committed, scored. Running and scoring now both stop.
  - **Git failures.** A git command that failed read as "no changes", so labels that
    were not committed could pass. It now stops the step.
  - **Redirects and `robots.txt`.** Only the origin's root was asked about, so an
    allowed root redirecting to a refused path or site was captured and visited. Each
    redirect and the landing address are now asked about too.
  - **Second annotator.** The draw could be made and kept from the pages of a run
    still in progress. It now waits until every stratum is complete or short.
- 2026-11-07: **correction, and a fresh start of the measurement.** Decided by the
  project owner on 2026-11-07 ("全部推奨どおり"), after the facts below were put to them.
  - **A visit did happen.** On 2026-11-04 (from 09:45 UTC) all four strata were visited
    from commit `7f90f26`, which was never pushed. This branch went on without it and
    without rule E5. So the entries above dated 2026-11-06 that say "before any visit"
    and "No held-out page had been visited" are **wrong**. They are left as written and
    corrected here. Their changes were made after that visit.
  - **That visit is not used.** It has no labels and has not been scored. Its plan and
    sample fingerprints are not this plan's, so the scorer refuses it anyway. It stays
    outside the repository. Nothing from it feeds this plan or sample.
  - **What was seen of it.** Nothing here records anyone reading WCC's results from that
    visit before these changes; that is unconfirmed, not shown. On 2026-11-07 its result
    file was opened only for its field names, its count and its hosts, in order to count
    sites drawn twice. Two hosts were one site under E4's new wording.
  - **E5 restored.** The two unpushed commits are merged, so the rule and its list are
    back, unchanged.
  - **Unsafe exposure.** The owner accepted the 2026-11-06 narrowing to capabilities
    WCC would run. Because it was made after the visit above, it is stated here. The
    threshold stays at 0, and the report still prints every bound capability and risk.
  - **E4.** The rule read "one site gets one page" but compared hosts. It now uses the
    pinned Public Suffix List, fetched from its own home (version in `plan.py`). Chosen
    over a hand list of parents (nobody here should choose them) and the `tldextract`
    package (its copy changes with the package version). The private section is kept
    because those are separately owned sites, which is how browsers treat them.
  - **Numbers.** The owner accepted the limits added during review: `robots.txt` at
    most 512 KiB within 40 s, and 180 s per candidate. They bound the tool's time and
    are not in the pass line.
  - **Sample.** Redrawn with the same seed and shuffle order, so a candidate survives
    unless E4 or E5 removes it. Many of the sites are therefore the ones visited on
    2026-11-04. WCC's code has not changed since then in the light of that visit. No
    threshold changed. The visit is repeated from the commit that holds this entry.
