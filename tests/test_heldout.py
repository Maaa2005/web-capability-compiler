"""The held-out evaluation's harness: drawing, visiting, labelling, scoring.

Everything but the last two tests runs without a network or a browser. Those
two run the real capture and the real WCC visit against the loopback fixtures,
which checks the harness and measures nothing: the fixtures are this project's
own pages.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from benchmarks.heldout import plan
from benchmarks.heldout.label import agreement_keys, ask_page, label_pages
from benchmarks.heldout.run import (
    CaptureRecord,
    RunFiles,
    WccRecord,
    capture,
    free_text_parameters,
    key_of,
    visit_candidates,
    wcc_visit,
)
from benchmarks.heldout.sample import (
    SAMPLE_PATH,
    Candidate,
    FrameMismatch,
    draw,
    exclusion,
    read_frame,
    site_key,
)
from benchmarks.heldout.score import (
    NotReady,
    cohen_kappa,
    metrics,
    score_files,
    score_page,
    select_scored,
    title_found,
    verdict,
    wilson,
)

# -- drawing -----------------------------------------------------------------


def test_the_exclusion_rules_are_the_plans() -> None:
    assert exclusion("http://shop.test") == "E1"
    assert exclusion("https://docs.python.org") == "E2"
    assert exclusion("https://ja.wikipedia.org") == "E2"
    assert exclusion("https://notpython.org") is None
    assert exclusion("https://free-porn.test") == "E3"
    assert exclusion("https://news.test") is None


def test_www_and_m_are_the_same_site() -> None:
    assert site_key("www.a.test") == site_key("m.a.test") == site_key("a.test") == "a.test"
    assert site_key("shop.a.test") == "shop.a.test"


def _rows() -> list[tuple[str, int]]:
    rows = [(f"https://site{i}.test", 1_000) for i in range(30)]
    rows += [(f"https://mid{i}.test", 5_000) for i in range(30)]
    rows += [(f"https://tail{i}.test", 500_000) for i in range(30)]
    rows += [("https://www.dup.test", 1_000), ("https://dup.test", 5_000)]
    return rows


def test_the_draw_is_repeatable_and_ordered_within_each_stratum() -> None:
    first, again = draw(_rows(), seed=7, per_stratum=5), draw(_rows(), seed=7, per_stratum=5)
    assert first.candidates == again.candidates
    assert draw(_rows(), seed=8, per_stratum=5).candidates != first.candidates
    by_stratum = Counter(c.stratum for c in first.candidates)
    assert by_stratum == {"top-1k": 5, "1k-10k": 5, "100k-1m": 5}
    for stratum in by_stratum:
        positions = [c.position for c in first.candidates if c.stratum == stratum]
        assert positions == [1, 2, 3, 4, 5]


def test_one_site_gets_one_page_across_strata() -> None:
    result = draw(_rows(), seed=7, per_stratum=40)
    keys = [site_key(c.host) for c in result.candidates]
    assert len(keys) == len(set(keys))
    assert result.excluded["E4"] == 1


def test_a_frame_file_that_is_not_the_plans_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "frame.csv.gz"
    path.write_bytes(gzip.compress(b"origin,rank\nhttps://a.test,1000\n"))
    with pytest.raises(FrameMismatch):
        read_frame(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert read_frame(path, expected_sha256=digest) == [("https://a.test", 1_000)]


def test_the_committed_sample_follows_the_plan() -> None:
    document = json.loads(SAMPLE_PATH.read_text())
    assert document["seed"] == plan.SEED == 0xBE444A34
    assert document["frame"]["sha256"] == plan.FRAME_SHA256
    candidates = [Candidate(**c) for c in document["candidates"]]
    assert len(candidates) == len(plan.STRATA) * plan.CANDIDATES_PER_STRATUM
    for stratum in plan.STRATA:
        mine = [c for c in candidates if c.stratum == stratum.name]
        assert [c.position for c in mine] == list(range(1, plan.CANDIDATES_PER_STRATUM + 1))
        assert all(c.rank_bucket in stratum.ranks for c in mine)
    assert all(exclusion(c.origin) is None for c in candidates)
    keys = [site_key(c.host) for c in candidates]
    assert len(keys) == len(set(keys))


# -- visiting ----------------------------------------------------------------


def _candidates(stratum: str = "top-1k", count: int = 6) -> list[Candidate]:
    return [Candidate(stratum, i, f"https://c{i}.test", 1_000) for i in range(1, count + 1)]


async def _fake_capture(url: str, shot: Path, text: Path, record: CaptureRecord) -> None:
    if "c2." in url:
        raise TimeoutError("never loaded")
    shot.write_bytes(b"png")
    text.write_text(f"text of {url}")
    record.final_url = url


async def _fake_visit(url: str, record: WccRecord) -> None:
    record.offered = [{"name": "submit_search_form", "category": "search", "labels": ["Go"]}]
    del url


async def test_visiting_replaces_refused_and_failed_candidates_in_order(tmp_path: Path) -> None:
    files = RunFiles(tmp_path)
    counts = await visit_candidates(
        files,
        _candidates(),
        robots=lambda url: "disallowed" if "c1." in url else None,
        take_capture=_fake_capture,
        visit=_fake_visit,
        pause_seconds=0,
        per_stratum=3,
        log=lambda _: None,
    )
    assert counts["top-1k"] == 3
    index = json.loads(files.index.read_text())
    assert [(r["key"], r["status"]) for r in index] == [
        ("top-1k/001", "robots"),
        ("top-1k/002", "failed"),
        ("top-1k/003", "captured"),
        ("top-1k/004", "captured"),
        ("top-1k/005", "captured"),
    ]
    assert len(json.loads(files.wcc.read_text())) == 3


async def test_an_excluded_page_makes_the_run_continue_down_the_order(tmp_path: Path) -> None:
    files = RunFiles(tmp_path)
    common: dict[str, Any] = {
        "robots": lambda url: None,
        "take_capture": _fake_capture,
        "visit": _fake_visit,
        "pause_seconds": 0,
        "per_stratum": 2,
        "log": lambda _: None,
    }
    await visit_candidates(files, _candidates(), **common)
    files.labels.write_text(json.dumps({"labels": {"top-1k/001": {"eligible": False}}}))
    counts = await visit_candidates(files, _candidates(), **common)
    assert counts["top-1k"] == 2
    keys = [r["key"] for r in json.loads(files.index.read_text())]
    assert keys == ["top-1k/001", "top-1k/002", "top-1k/003", "top-1k/004"]


# -- labelling ---------------------------------------------------------------


def _scripted(*answers: str) -> Any:
    queue = list(answers)
    return lambda prompt: queue.pop(0)


def test_the_five_questions_produce_a_label() -> None:
    label = ask_page(
        _scripted("y", "y", "y", "First title", "Second title", "n", "y", "Sign up", "")
    )
    assert label == {
        "eligible": True,
        "exclusion": None,
        "search": True,
        "result_list": True,
        "titles": ["First title", "Second title"],
        "next_page": False,
        "state_changing": True,
        "controls": ["Sign up"],
    }
    assert ask_page(_scripted("n", "2")) == {"eligible": False, "exclusion": "login-or-age-wall"}


def test_labelling_never_reads_wccs_results(tmp_path: Path) -> None:
    files = RunFiles(tmp_path)
    files.captures.mkdir(parents=True)
    (files.captures / "top-1k-001.txt").write_text("Welcome\nSearch")
    record = CaptureRecord(
        "top-1k/001", "top-1k", 1, "https://a.test", "https://a.test/", status="captured"
    )
    record.screenshot, record.text = "top-1k-001.png", "top-1k-001.txt"
    files.index.write_text(json.dumps([record.__dict__]))
    # A directory where the results file would be: reading it would raise.
    files.wcc.mkdir(parents=True)

    shown: list[str] = []
    path = label_pages(files, "tester", ask=_scripted("y", "y", "n", "n", "n"), say=shown.append)
    labels = json.loads(path.read_text())
    assert labels["annotator"] == "tester" and labels["blind_to_wcc"] is True
    assert labels["labels"]["top-1k/001"]["search"] is True
    assert not any("results" in line for line in shown)


def test_the_agreement_pages_are_drawn_with_the_seed() -> None:
    keys = [f"top-1k/{i:03d}" for i in range(1, 41)]
    chosen = agreement_keys(keys)
    assert chosen == agreement_keys(list(reversed(keys)))
    assert len(chosen) == plan.AGREEMENT_PAGES and set(chosen) <= set(keys)


# -- scoring -----------------------------------------------------------------


def test_titles_match_after_normalisation_or_by_long_containment() -> None:
    labels = ["A Light in the Attic £51.77", "ＦＵＬＬ ＷＩＤＴＨ", "Tipping"]
    assert title_found("a light in the  attic", labels)
    assert title_found("full width", labels)
    assert title_found("Tipping", labels)
    assert not title_found("Tip", labels)  # too short to count by containment
    assert not title_found("", labels)


def test_wilson_matches_the_published_values() -> None:
    low, high = wilson(13, 17) or (0, 0)
    assert (round(low, 3), round(high, 3)) == (0.527, 0.904)
    assert round((wilson(0, 50) or (0, 0))[1], 3) == 0.071
    assert wilson(0, 0) is None


def _label(**overrides: Any) -> dict[str, Any]:
    label = {
        "eligible": True,
        "search": False,
        "result_list": False,
        "titles": [],
        "next_page": False,
        "state_changing": False,
        "controls": [],
    }
    return label | overrides


def _wcc(offered: list[dict[str, Any]], result_list: list[str] | None = None) -> WccRecord:
    return WccRecord("k", "https://a.test/", offered=offered, result_list=result_list or [])


def test_a_result_list_counts_only_if_both_titles_are_in_it() -> None:
    offered = [{"name": "open_result_item", "category": "navigate", "labels": []}]
    label = _label(result_list=True, titles=["Story one is here", "Story two is here"])
    right = score_page(
        "k", "s", "o", label, _wcc(offered, ["Story one is here", "Story two is here"])
    )
    wrong = score_page("k", "s", "o", label, _wcc(offered, ["About us", "Contact"]))
    assert right.results_matched and not wrong.results_matched
    assert wrong.results_offered and wrong.results_labelled


def test_a_filter_with_a_free_text_box_answers_search_and_either_pager_answers_next() -> None:
    keyword = {"name": "apply_filter_form", "category": "filter", "text_parameters": 1}
    brand_only = {"name": "apply_filter_form", "category": "filter", "text_parameters": 0}
    label = _label(search=True, next_page=True)
    assert score_page("k", "s", "o", label, _wcc([keyword])).search_right
    assert not score_page("k", "s", "o", label, _wcc([brand_only])).search_offered
    for name in ("navigate_next_page", "navigate_to_page"):
        pager = {"name": name, "category": "navigate"}
        assert score_page("k", "s", "o", label, _wcc([pager])).next_right
    previous = {"name": "navigate_previous_page", "category": "navigate"}
    assert not score_page("k", "s", "o", label, _wcc([previous])).next_offered


def test_free_text_parameters_are_strings_without_a_fixed_list() -> None:
    schema: dict[str, object] = {
        "properties": {
            "keyword": {"type": "string"},
            "brand": {"type": "string", "enum": ["A", "B"]},
            "in_stock": {"type": "boolean"},
        }
    }
    assert free_text_parameters(schema) == 1
    assert free_text_parameters({}) == 0


def test_a_capability_over_a_labelled_state_changing_control_is_unsafe() -> None:
    offered = [{"name": "submit_search_form", "category": "search", "labels": ["Subscribe"]}]
    page = score_page("k", "s", "o", _label(controls=["subscribe"]), _wcc(offered))
    assert page.unsafe == ["submit_search_form: Subscribe"]
    safe = score_page("k", "s", "o", _label(controls=["Log in"]), _wcc(offered))
    assert safe.unsafe == []


def _pages(search_right: int, search_wrong: int, search_missed: int) -> list[Any]:
    search = [{"name": "submit_search_form", "category": "search", "labels": []}]
    pages = [score_page("k", "s", "o", _label(search=True), _wcc(search))] * search_right
    pages += [score_page("k", "s", "o", _label(), _wcc(search))] * search_wrong
    pages += [score_page("k", "s", "o", _label(search=True), _wcc([]))] * search_missed
    return pages


def test_the_verdict_follows_the_thresholds() -> None:
    passing = metrics(_pages(search_right=18, search_wrong=1, search_missed=2))
    assert passing["search"]["precision"]["value"] == pytest.approx(18 / 19)
    assert passing["search"]["recall"]["value"] == pytest.approx(18 / 20)
    outcome = verdict(passing)
    assert outcome["criteria"]["T2 search P >= 0.90 and R >= 0.60"] == "pass"
    # No result lists at all: T3 cannot be judged, so the run is not a pass.
    assert outcome["criteria"]["T3 result list P >= 0.70 and R >= 0.40"] == "not judged"
    assert outcome["overall"] == "not judged"

    failing = verdict(metrics(_pages(search_right=10, search_wrong=5, search_missed=0)))
    assert failing["criteria"]["T2 search P >= 0.90 and R >= 0.60"] == "fail"
    assert failing["overall"] == "fail"


def test_one_unsafe_exposure_fails_the_run_whatever_else_holds() -> None:
    offered = [{"name": "submit_search_form", "category": "search", "labels": ["Join"]}]
    pages = _pages(search_right=20, search_wrong=0, search_missed=0)
    pages.append(score_page("k", "s", "o", _label(controls=["Join"]), _wcc(offered)))
    assert verdict(metrics(pages))["overall"] == "fail"


def test_scoring_takes_the_first_eligible_pages_and_refuses_an_unlabelled_one() -> None:
    candidates = _candidates(count=5)
    index = {
        key_of(c): CaptureRecord(key_of(c), c.stratum, c.position, c.origin, c.url, "captured")
        for c in candidates
    }
    index["top-1k/001"].status = "robots"
    results = {key: WccRecord(key, "u") for key in index}
    labels = {"top-1k/002": _label(eligible=False), "top-1k/003": _label(), "top-1k/004": _label()}
    scored, tally = select_scored(candidates, index, results, labels, per_stratum=2)
    assert [key_of(c) for c in scored] == ["top-1k/003", "top-1k/004"]
    assert tally["top-1k"] == {
        "used": 4,
        "robots": 1,
        "failed": 0,
        "wcc_error": 0,
        "excluded": 1,
        "scored": 2,
    }
    with pytest.raises(NotReady):
        select_scored(candidates, index, results, labels, per_stratum=3)


def test_cohens_kappa() -> None:
    assert cohen_kappa([True, False, True, False], [True, False, True, False]) == 1.0
    assert cohen_kappa([True, True, False, False], [True, False, True, False]) == 0.0


# -- the real capture and visit, on this project's own fixtures ------------------


async def test_the_capture_keeps_what_a_person_sees(fixture_server: str, tmp_path: Path) -> None:
    url = f"{fixture_server}/search-page/"
    record = CaptureRecord("top-1k/001", "top-1k", 1, fixture_server, url)
    shot, text = tmp_path / "shot.png", tmp_path / "text.txt"
    await capture(url, shot, text, record, allow_private_network=True)
    assert shot.read_bytes().startswith(b"\x89PNG")
    assert "Search" in text.read_text()
    assert record.http_status == 200 and record.final_url == url


async def test_the_capture_refuses_what_the_url_policy_refuses(
    fixture_server: str, tmp_path: Path
) -> None:
    record = CaptureRecord("k", "s", 1, fixture_server, fixture_server)
    with pytest.raises(Exception, match="ERR_BLOCKED_BY_CLIENT"):
        await capture(
            f"{fixture_server}/search-page/", tmp_path / "s.png", tmp_path / "t.txt", record
        )


async def test_the_whole_harness_runs_on_the_fixtures(fixture_server: str, tmp_path: Path) -> None:
    """A dry run: visit, label by script, score. It checks the plumbing only."""
    pages = ["search-page", "product-list", "filter-page"]
    candidates = [
        Candidate("top-1k", i, f"{fixture_server}/{name}", 1_000)
        for i, name in enumerate(pages, start=1)
    ]

    async def private_capture(url: str, shot: Path, text: Path, record: CaptureRecord) -> None:
        await capture(url, shot, text, record, allow_private_network=True)

    async def private_visit(url: str, record: WccRecord) -> None:
        await wcc_visit(url, record, allow_private_network=True)

    files = RunFiles(tmp_path)
    await visit_candidates(
        files,
        candidates,
        robots=lambda url: None,
        take_capture=private_capture,
        visit=private_visit,
        pause_seconds=0,
        per_stratum=3,
        log=lambda _: None,
    )
    results = json.loads(files.wcc.read_text())
    assert all(r["error"] is None for r in results), results
    search = next(r for r in results if r["key"] == "top-1k/001")
    assert any(o["category"] == "search" and o["labels"] for o in search["offered"])

    files.labels.write_text(
        json.dumps(
            {
                "annotator": "script",
                "labels": {
                    "top-1k/001": _label(search=True),
                    "top-1k/002": _label(),
                    "top-1k/003": _label(search=True),
                },
            }
        )
    )
    document = score_files(files, candidates, per_stratum=3)
    assert document["metrics"]["pages"] == 3
    assert document["inputs"]["labels.json"]
    assert document["verdict"]["overall"] in {"pass", "fail", "not judged"}


# -- E5: adult and gambling sites, from a pinned public list ---------------------


def test_a_host_is_blocked_by_itself_or_by_a_parent_domain() -> None:
    from benchmarks.heldout.sample import blocked

    domains = frozenset({"casino.test", "bad.example.org"})
    assert blocked("casino.test", domains)
    assert blocked("www.casino.test", domains)
    assert blocked("a.b.casino.test", domains)
    assert blocked("bad.example.org", domains)
    assert not blocked("example.org", domains)
    assert not blocked("notcasino.test", domains)
    assert not blocked("test", frozenset({"test"})), "a bare top-level name is never matched"


def test_the_blocklist_rule_is_e5_and_comes_after_the_plans_other_rules() -> None:
    blocklist = frozenset({"casino.test"})
    assert exclusion("https://www.casino.test", blocklist) == "E5"
    assert exclusion("http://casino.test", blocklist) == "E1"
    assert exclusion("https://news.test", blocklist) is None


def test_a_blocklist_file_with_the_wrong_hash_is_refused(tmp_path: Path) -> None:
    from benchmarks.heldout.sample import BlocklistMismatch, read_blocklist

    path = tmp_path / "list.txt"
    path.write_text("casino.test\n# comment\n\n0.0.0.0 hosts.test\n")
    with pytest.raises(BlocklistMismatch):
        read_blocklist(path, "0" * 64)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert read_blocklist(path, digest) == {"casino.test", "hosts.test"}


def test_the_committed_blocklist_is_the_one_the_plan_names() -> None:
    from benchmarks.heldout import plan
    from benchmarks.heldout.sample import BLOCKLIST_PATH

    assert hashlib.sha256(BLOCKLIST_PATH.read_bytes()).hexdigest() == plan.BLOCKLISTS[0].sha256


def test_the_committed_sample_contains_no_host_on_the_committed_blocklist() -> None:
    from benchmarks.heldout import plan
    from benchmarks.heldout.sample import BLOCKLIST_PATH, blocked, load_sample, read_blocklist

    domains = read_blocklist(BLOCKLIST_PATH, plan.BLOCKLISTS[0].sha256)
    assert not [c.origin for c in load_sample() if blocked(c.host, domains)]
