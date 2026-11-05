"""The held-out evaluation's harness: drawing, visiting, labelling, scoring.

No test reaches past this machine. The ones that open a browser or a socket run
the real capture, the real ``robots.txt`` read and the real WCC visit against
loopback servers, which checks the harness and measures nothing: the fixtures are
this project's own pages.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import functools
import gzip
import hashlib
import http.server
import inspect
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from benchmarks.heldout import plan
from benchmarks.heldout import run as run_module
from benchmarks.heldout import score as score_module
from benchmarks.heldout.__main__ import _printing_never_fails, main
from benchmarks.heldout.label import agreement_keys, ask_page, label_pages, scored_keys
from benchmarks.heldout.run import (
    REPO_ROOT,
    CaptureRecord,
    RobotsUnreachable,
    RunFiles,
    WccRecord,
    _commit_mismatch,
    _shut_down,
    capture,
    code_sha256,
    free_text_parameters,
    guarded_robots_refusal,
    key_of,
    load_index,
    load_wcc,
    run_all,
    run_metadata,
    visit_candidates,
    wcc_visit,
)
from benchmarks.heldout.sample import (
    SAMPLE_PATH,
    Candidate,
    FrameMismatch,
    draw,
    exclusion,
    landing_exclusion,
    read_frame,
    site_key,
)
from benchmarks.heldout.score import (
    NotReady,
    cohen_kappa,
    metrics,
    report,
    score_files,
    score_page,
    score_run,
    select_scored,
    stratum_states,
    title_found,
    verdict,
    wilson,
)
from benchmarks.measure_coverage import robots_refusal

from wcc.driver.base import NavigationBlockedError
from wcc.driver.egress_proxy import EgressProxy
from wcc.models.capability import RiskLevel

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
        "unreachable": 0,
        "landed_elsewhere": 0,
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
    assert "Search" in text.read_text(encoding="utf-8")
    assert record.http_status == 200 and record.final_url == url


async def test_the_capture_refuses_what_the_url_policy_refuses(
    fixture_server: str, tmp_path: Path
) -> None:
    record = CaptureRecord("k", "s", 1, fixture_server, fixture_server)
    with pytest.raises(NavigationBlockedError, match="loopback"):
        await capture(
            f"{fixture_server}/search-page/", tmp_path / "s.png", tmp_path / "t.txt", record
        )


async def _private_capture(url: str, shot: Path, text: Path, record: CaptureRecord) -> None:
    await capture(url, shot, text, record, allow_private_network=True)


async def _private_visit(url: str, record: WccRecord) -> None:
    await wcc_visit(url, record, allow_private_network=True)


def _one_host(final_url: str, kept: dict[str, str]) -> None:
    """Every loopback candidate has one host, so E4 would rule out all but the first."""
    del final_url, kept


_FIXTURE_PAGES = ("search-page", "product-list", "filter-page")


def _fixture_candidates(base: str) -> list[Candidate]:
    return [
        Candidate("top-1k", i, f"{base}/{name}", 1_000)
        for i, name in enumerate(_FIXTURE_PAGES, start=1)
    ]


def _assert_the_fixtures_score_as_labelled(document: dict[str, Any]) -> None:
    """What these three pages must score, given the labels both dry runs write."""
    m = document["metrics"]
    assert m["pages"] == 3
    expected = {"search": (2, 2, 2, 2), "result_list": (1, 1, 1, 1), "next_page": (1, 1, 1, 1)}
    for kind, (precision_k, precision_n, recall_k, recall_n) in expected.items():
        assert (m[kind]["precision"]["k"], m[kind]["precision"]["n"]) == (precision_k, precision_n)
        assert (m[kind]["recall"]["k"], m[kind]["recall"]["n"]) == (recall_k, recall_n)
        assert m[kind]["precision"]["judged"] is False
    assert m["unsafe_exposures"]["pages"] == 0
    assert m["any_capability"]["label_denominator"]["k"] == 3
    criteria = document["verdict"]["criteria"]
    assert list(criteria.values()) == ["pass", "not judged", "not judged"]
    assert document["verdict"]["overall"] == "not judged"
    assert document["accounting"]["top-1k"]["scored"] == 3
    by_key = {page["key"]: page for page in document["pages"]}
    assert by_key["top-1k/001"]["search_right"] and not by_key["top-1k/001"]["results_offered"]
    assert by_key["top-1k/002"]["results_matched"] and by_key["top-1k/002"]["next_right"]
    assert by_key["top-1k/003"]["search_right"]


async def test_the_whole_harness_runs_on_the_fixtures(fixture_server: str, tmp_path: Path) -> None:
    """A dry run: visit, label by script, score. It checks the plumbing only."""
    candidates = _fixture_candidates(fixture_server)
    files = RunFiles(tmp_path)
    await visit_candidates(
        files,
        candidates,
        robots=lambda url: None,
        take_capture=_private_capture,
        visit=_private_visit,
        pause_seconds=0,
        per_stratum=3,
        log=lambda _: None,
        landing=_one_host,
    )
    results = json.loads(files.wcc.read_text())
    assert [r["error"] for r in results] == [None, None, None]
    assert [r.wcc_complete for r in load_index(files).values()] == [True, True, True]
    search = next(r for r in results if r["key"] == "top-1k/001")
    form = next(o for o in search["offered"] if o["category"] == "search")
    assert "Search" in form["labels"] and "Search" in form["names"]
    products = next(r for r in results if r["key"] == "top-1k/002")
    assert products["result_list"] == [
        "Wireless Earbuds",
        "Noise Cancelling Headphones",
        "Bluetooth Speaker",
    ]

    files.labels.write_text(
        json.dumps(
            {
                "annotator": "script",
                "labels": {
                    "top-1k/001": _label(search=True),
                    "top-1k/002": _label(
                        result_list=True,
                        titles=["Wireless Earbuds", "Bluetooth Speaker"],
                        next_page=True,
                    ),
                    "top-1k/003": _label(search=True),
                },
            }
        )
    )
    document = score_files(files, candidates, per_stratum=3)
    _assert_the_fixtures_score_as_labelled(document)
    assert document["inputs"]["labels.json"]
    assert document["strata"]["top-1k"] == "complete"
    assert document["verdict"]["unfinished_strata"] == ["1k-10k", "10k-100k", "100k-1m"]


# -- the capture and robots.txt stay inside the URL policy -----------------------


class _FirstLoopbackOnly:
    """A policy for two loopback addresses: 127.0.0.1 is "public", the rest internal."""

    def check(self, url: str, *, allow_private_network: bool = False) -> None:
        del allow_private_network
        if urlsplit(url).hostname != "127.0.0.1":
            raise NavigationBlockedError(f"{url} is internal")


class _Internal:
    """A service on 127.0.0.2, recording every connection that reaches it."""

    def __init__(self) -> None:
        self.hits: list[bytes] = []
        self._socket = socket.socket()
        self._socket.bind(("127.0.0.2", 0))
        self._socket.listen(16)
        self.port: int = self._socket.getsockname()[1]
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        with contextlib.suppress(OSError):
            while True:
                connection, _ = self._socket.accept()
                with connection:
                    connection.settimeout(1)
                    with contextlib.suppress(OSError):
                        self.hits.append(connection.recv(200))
                        connection.sendall(
                            b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                        )

    def close(self) -> None:
        self._socket.close()


@pytest.fixture
def internal() -> Iterator[_Internal]:
    try:
        service = _Internal()
    except OSError:
        pytest.skip("this machine does not route 127.0.0.2 to loopback")
    yield service
    service.close()


Routes = dict[str, tuple[int, dict[str, str], bytes]]


@pytest.fixture
def serve() -> Iterator[Callable[[Routes], str]]:
    """Serve fixed answers on 127.0.0.1 and yield a function that starts a server."""
    servers: list[http.server.ThreadingHTTPServer] = []

    def start(routes: Routes) -> str:
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                pass

            def do_GET(self) -> None:  # noqa: N802
                status, headers, body = routes.get(self.path, (404, {}, b""))
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


async def test_the_capture_cannot_reach_a_refused_address_by_websocket_or_worker(
    serve: Callable[[Routes], str],
    internal: _Internal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``context.route`` sees neither socket here; a capture with only that reached both."""
    monkeypatch.setattr(run_module, "UrlPolicy", _FirstLoopbackOnly)
    started: list[object] = []
    real_start = EgressProxy.start

    async def spy(proxy: EgressProxy) -> str:
        started.append(proxy)
        return await real_start(proxy)

    monkeypatch.setattr(EgressProxy, "start", spy)
    socket_at = f"ws://127.0.0.2:{internal.port}"
    page = f"""<html><title>t</title><body>page<script>
      new WebSocket('{socket_at}/from-page');
      const worker = new Worker('/worker.js');
      worker.onmessage = (event) => document.body.append(event.data);
    </script></body></html>"""
    script = f"new WebSocket('{socket_at}/from-worker'); postMessage('worker-ran');"
    base = serve(
        {
            "/": (200, {"Content-Type": "text/html"}, page.encode()),
            "/worker.js": (200, {"Content-Type": "text/javascript"}, script.encode()),
        }
    )
    record = CaptureRecord("k", "s", 1, base, f"{base}/")
    text = tmp_path / "t.txt"
    await capture(f"{base}/", tmp_path / "s.png", text, record, allow_private_network=True)
    await asyncio.sleep(0.2)
    assert "worker-ran" in text.read_text(encoding="utf-8")
    assert started
    assert internal.hits == []
    assert record.http_status == 200


def test_robots_never_follows_a_redirect_to_a_refused_address(
    serve: Callable[[Routes], str], internal: _Internal, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_module, "UrlPolicy", _FirstLoopbackOnly)
    # ``no_proxy`` is what makes urllib's own ProxyHandler go direct.
    monkeypatch.setenv("no_proxy", "*")
    for target in (
        f"http://127.0.0.2:{internal.port}/secret",
        f"https://127.0.0.2:{internal.port}/secret",
        f"ftp://127.0.0.2:{internal.port}/secret",
    ):
        base = serve({"/robots.txt": (302, {"Location": target}, b"")})
        answer = guarded_robots_refusal(f"{base}/")
        assert answer is not None and "disallows" in answer, target
    assert internal.hits == []


def test_robots_refuses_an_address_the_policy_refuses_before_any_connection(
    internal: _Internal, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The real policy, with no allowance: 127.0.0.2 is loopback.
    answer = guarded_robots_refusal(f"http://127.0.0.2:{internal.port}/")
    assert answer is not None and "refused by the URL policy" in answer
    monkeypatch.setattr(run_module, "UrlPolicy", _FirstLoopbackOnly)
    answer = guarded_robots_refusal(f"https://127.0.0.2:{internal.port}/")
    assert answer is not None and "refused by the URL policy" in answer
    assert internal.hits == []


def test_robots_over_https_tunnels_to_the_origin_and_speaks_tls_itself(
    internal: _Internal,
) -> None:
    """The proxy decrypts nothing: what arrives at the origin is the client's handshake."""
    with pytest.raises(RobotsUnreachable):
        guarded_robots_refusal(f"https://127.0.0.2:{internal.port}/", allow_private_network=True)
    assert internal.hits and internal.hits[0][:1] == b"\x16"  # a TLS handshake record


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (200, b"User-agent: *\nDisallow: /\n"),
        (200, b"User-agent: *\nDisallow: /private\n"),
        (302, b""),  # followed to a file that allows
        (404, b""),
        (401, b""),
        (403, b""),
        (500, b""),
    ],
)
def test_guarded_robots_answers_as_the_benchmarks_reader_does(
    serve: Callable[[Routes], str],
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: bytes,
) -> None:
    monkeypatch.setenv("no_proxy", "*")
    headers = {"Location": "/moved.txt"} if status == 302 else {}
    base = serve(
        {
            "/robots.txt": (status, headers, body),
            "/moved.txt": (200, {}, b"User-agent: *\nAllow: /\n"),
        }
    )
    url = f"{base}/"
    assert guarded_robots_refusal(url, allow_private_network=True) == robots_refusal(url)


def test_an_unreachable_robots_file_disallows_as_the_benchmarks_reader_does() -> None:
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
    # Neither opens the page. Only the reason differs: nothing here was refused.
    with pytest.raises(RobotsUnreachable):
        guarded_robots_refusal(f"http://127.0.0.1:{port}/", allow_private_network=True)
    assert robots_refusal(f"http://127.0.0.1:{port}/") is not None


def test_the_run_of_record_reads_robots_through_the_guard() -> None:
    default = inspect.signature(visit_candidates).parameters["robots"].default
    assert default is guarded_robots_refusal


# -- a run that has not finished cannot pass --------------------------------------


def _all_candidates() -> list[Candidate]:
    return [
        Candidate(s.name, i, f"https://{s.name}-{i}.test", s.ranks[0])
        for s in plan.STRATA
        for i in range(1, plan.CANDIDATES_PER_STRATUM + 1)
    ]


def _write_run(tmp_path: Path, pages: dict[str, int], *, offered: list[dict[str, Any]]) -> RunFiles:
    """A run with ``pages[stratum]`` right, labelled pages at the front of each stratum."""
    files = RunFiles(tmp_path)
    files.captures.mkdir(parents=True)
    files.wcc.parent.mkdir(parents=True)
    index, results, labels = [], [], {}
    titles = ["The first long title", "The second long title"]
    for candidate in _all_candidates():
        if candidate.position > pages.get(candidate.stratum, 0):
            continue
        key = key_of(candidate)
        index.append(
            asdict(
                CaptureRecord(
                    key,
                    candidate.stratum,
                    candidate.position,
                    candidate.origin,
                    candidate.url,
                    "captured",
                )
            )
        )
        results.append(asdict(WccRecord(key, candidate.url, offered=offered, result_list=titles)))
        labels[key] = _label(search=True, result_list=True, titles=titles, controls=["Join"])
    files.index.write_text(json.dumps(index), encoding="utf-8")
    files.wcc.write_text(json.dumps(results), encoding="utf-8")
    files.labels.write_text(json.dumps({"annotator": "t", "labels": labels}), encoding="utf-8")
    return files


_RIGHT = [
    {"name": "submit_search_form", "category": "search", "risk": "low", "labels": ["Go"]},
    {"name": "open_result_item", "category": "navigate", "risk": "low", "labels": []},
]


def test_one_stratum_of_good_pages_is_not_a_pass(tmp_path: Path) -> None:
    files = _write_run(tmp_path, {"top-1k": 10}, offered=_RIGHT)
    document = score_files(files, _all_candidates())
    assert set(document["verdict"]["criteria"].values()) == {"pass"}
    assert document["verdict"]["overall"] == "not judged"
    assert document["verdict"]["unfinished_strata"] == ["top-1k", "1k-10k", "10k-100k", "100k-1m"]
    assert document["strata"]["1k-10k"] == "unfinished"
    assert "not finished, so the run cannot pass" in report(document)


def test_every_stratum_at_its_number_can_pass(tmp_path: Path) -> None:
    files = _write_run(tmp_path, {s.name: 3 for s in plan.STRATA}, offered=_RIGHT)
    document = score_files(files, _all_candidates(), per_stratum=3)
    assert set(document["strata"].values()) == {"complete"}
    assert document["verdict"]["overall"] == "pass"


def test_a_stratum_that_used_every_candidate_is_short_and_does_not_block(tmp_path: Path) -> None:
    files = _write_run(tmp_path, {s.name: 3 for s in plan.STRATA}, offered=_RIGHT)
    index = json.loads(files.index.read_text(encoding="utf-8"))
    for candidate in _all_candidates():
        if candidate.stratum == "top-1k" and candidate.position > 3:
            record = CaptureRecord(
                key_of(candidate),
                candidate.stratum,
                candidate.position,
                candidate.origin,
                candidate.url,
                "robots",
            )
            index.append(asdict(record))
    files.index.write_text(json.dumps(index), encoding="utf-8")
    document = score_files(files, _all_candidates(), per_stratum=4)
    assert document["strata"]["top-1k"] == "short"
    assert document["strata"]["1k-10k"] == "unfinished"
    assert document["verdict"]["unfinished_strata"] == ["1k-10k", "10k-100k", "100k-1m"]
    assert "used every candidate and came up short" in report(document)


def test_a_candidate_list_smaller_than_the_plans_does_not_count_as_used_up() -> None:
    candidates = _candidates(count=2)
    accounting = {s.name: {"used": 0, "scored": 0} for s in plan.STRATA}
    accounting["top-1k"] = {"used": 2, "scored": 0}
    assert stratum_states(candidates, accounting)["top-1k"] == "unfinished"


def test_an_unfinished_run_still_fails_on_an_unsafe_exposure(tmp_path: Path) -> None:
    unsafe = [{"name": "submit_search_form", "category": "search", "labels": ["Join"]}]
    files = _write_run(tmp_path, {"top-1k": 10}, offered=[*_RIGHT, *unsafe])
    assert score_files(files, _all_candidates())["verdict"]["overall"] == "fail"


# -- T1 counts what WCC would run --------------------------------------------------


@pytest.mark.parametrize("risk", ["medium", "high", "critical"])
def test_a_capability_wcc_refuses_to_run_is_not_an_unsafe_exposure(risk: str) -> None:
    offered = [{"name": "subscribe", "category": "submit", "risk": risk, "labels": ["Join"]}]
    assert score_page("k", "s", "o", _label(controls=["Join"]), _wcc(offered)).unsafe == []


@pytest.mark.parametrize("risk", [{"risk": "low"}, {}, {"risk": "unheard-of"}])
def test_a_capability_that_could_run_is_an_unsafe_exposure(risk: dict[str, str]) -> None:
    """A level nobody recognises, or none at all, is not evidence that it cannot run."""
    offered = [{"name": "subscribe", "category": "submit", "labels": ["Join"]} | risk]
    page = score_page("k", "s", "o", _label(controls=["Join"]), _wcc(offered))
    assert page.unsafe == ["subscribe: Join"]


def test_the_plans_refused_levels_are_every_level_but_low() -> None:
    assert set(plan.NON_EXECUTABLE_RISKS) == {str(r) for r in RiskLevel} - {str(RiskLevel.LOW)}


def test_the_report_prints_each_capabilitys_risk(tmp_path: Path) -> None:
    offered = [{"name": "subscribe", "category": "submit", "risk": "high", "labels": ["Join"]}]
    files = _write_run(tmp_path, {"top-1k": 1}, offered=offered)
    assert "subscribe [high] (Join)" in report(score_files(files, _all_candidates()))


# -- resuming a run ---------------------------------------------------------------


@pytest.fixture
def visits(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Replace the visits with a count of them, and git with a fixed answer."""
    called: list[int] = []

    async def no_visits(files: RunFiles, candidates: list[Candidate]) -> dict[str, int]:
        del files
        called.append(len(candidates))
        return {s.name: 0 for s in plan.STRATA}

    monkeypatch.setattr(run_module, "visit_candidates", no_visits)
    monkeypatch.setattr(run_module, "_git", lambda *args: "abc" if args[0] == "rev-parse" else "")
    return called


def test_a_run_resumes_under_the_conditions_it_started_with(
    tmp_path: Path, visits: list[int]
) -> None:
    assert run_all(tmp_path) == 0
    assert run_all(tmp_path) == 0
    assert len(visits) == 2


@pytest.mark.parametrize(
    "key", ["commit", "code_sha256", "plan_sha256", "plan_py_sha256", "sample_sha256"]
)
@pytest.mark.parametrize("change", ["differs", "missing"])
def test_a_run_stops_when_a_fingerprint_does_not_match(
    tmp_path: Path, visits: list[int], capsys: pytest.CaptureFixture[str], key: str, change: str
) -> None:
    assert run_all(tmp_path) == 0
    meta = RunFiles(tmp_path).meta
    recorded = json.loads(meta.read_text(encoding="utf-8"))
    if change == "differs":
        recorded[key] = "0" * 40
    else:
        del recorded[key]
    written = json.dumps(recorded)
    meta.write_text(written, encoding="utf-8")
    capsys.readouterr()

    assert run_all(tmp_path) == 2
    assert len(visits) == 1
    assert meta.read_text(encoding="utf-8") == written
    assert "not resumed" in capsys.readouterr().out


def test_results_without_a_run_file_are_not_resumed(tmp_path: Path, visits: list[int]) -> None:
    files = RunFiles(tmp_path)
    files.captures.mkdir(parents=True)
    files.index.write_text("[]", encoding="utf-8")
    assert run_all(tmp_path) == 2
    assert visits == []
    assert not files.meta.exists()


def test_a_commit_that_only_touches_the_runs_own_directory_is_not_a_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = REPO_ROOT / "benchmarks" / "results" / "heldout-x"
    changed = "benchmarks/results/heldout-x/labels.json"
    monkeypatch.setattr(run_module, "_git", lambda *args: changed)
    assert _commit_mismatch("old", "new", out) is None

    changed = f"{changed}\nsrc/wcc/compiler/risk.py"
    assert _commit_mismatch("old", "new", out) is not None
    changed = ""  # git could not say what changed
    assert _commit_mismatch("old", "new", out) is not None
    changed = "labels.json"
    assert _commit_mismatch("old", "new", Path("/elsewhere/run")) is not None
    assert _commit_mismatch("", "", out) is not None


@pytest.mark.parametrize(
    "edited", ["src/wcc/a.py", "benchmarks/heldout/run.py", "benchmarks/measure_coverage.py"]
)
def test_uncommitted_code_changes_the_code_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, edited: str
) -> None:
    for name in ("src/wcc/a.py", "benchmarks/heldout/run.py", "benchmarks/measure_coverage.py"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("original", encoding="utf-8")
    monkeypatch.setattr(run_module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(run_module, "__file__", str(tmp_path / "benchmarks/heldout/run.py"))
    before = code_sha256()
    assert before == code_sha256()
    (tmp_path / edited).write_text("edited", encoding="utf-8")
    assert code_sha256() != before


# -- text is UTF-8 whatever the machine's locale -----------------------------------


def test_every_text_file_in_the_harness_names_its_encoding() -> None:
    package = Path(plan.__file__).parent
    for path in package.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            called = node.func
            # Path.read_text, Path.write_text and the builtin open. ``opener.open`` is
            # urllib's, and takes no encoding.
            is_text_file = (
                isinstance(called, ast.Attribute) and called.attr in {"read_text", "write_text"}
            ) or (isinstance(called, ast.Name) and called.id == "open")
            if is_text_file:
                named = {keyword.arg for keyword in node.keywords}
                assert "encoding" in named, f"{path.name}:{node.lineno}"


_NOT_IN_CP932 = "caf\u00e9 \U0001f600 \u20a9 \u2014"


async def test_page_text_outside_the_locales_encoding_is_captured_as_utf8(
    serve: Callable[[Routes], str], tmp_path: Path
) -> None:
    page = f"<html><title>{_NOT_IN_CP932}</title><body>{_NOT_IN_CP932}</body></html>"
    base = serve({"/": (200, {"Content-Type": "text/html; charset=utf-8"}, page.encode())})
    record = CaptureRecord("k", "s", 1, base, f"{base}/")
    text = tmp_path / "t.txt"
    await capture(f"{base}/", tmp_path / "s.png", text, record, allow_private_network=True)
    assert text.read_bytes().decode("utf-8") == _NOT_IN_CP932
    assert record.title == _NOT_IN_CP932


def test_the_run_files_round_trip_with_the_utf8_mode_off(tmp_path: Path) -> None:
    """Python's own default on a Japanese Windows, which a developer's PYTHONUTF8 hides."""
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from benchmarks.heldout.run import CaptureRecord, RunFiles, WccRecord, _write\n"
        "from benchmarks.heldout.run import load_index, load_wcc\n"
        "title = '\\U0001f600'\n"
        "files = RunFiles(Path(sys.argv[1]))\n"
        "_write(files.index, [CaptureRecord('k', 's', 1, 'o', 'u', title=title)])\n"
        "_write(files.wcc, [WccRecord('k', 'u', unbound=[title])])\n"
        "assert load_index(files)['k'].title == title\n"
        "assert load_wcc(files)['k'].unbound == [title]\n"
    )
    done = subprocess.run(
        [sys.executable, "-X", "utf8=0", "-c", script, str(tmp_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env={k: v for k, v in os.environ.items() if k != "PYTHONUTF8"},
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "\U0001f600" in RunFiles(tmp_path).index.read_bytes().decode("utf-8")


def test_the_cli_prints_what_the_console_cannot_encode(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp932", write_through=True)
    monkeypatch.setattr(sys, "stdout", console)
    _printing_never_fails()
    print(_NOT_IN_CP932)
    assert b"\\U0001f600" in raw.getvalue()


# -- a lost connection is not a refusal --------------------------------------------


def _closed_port() -> int:
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        return unused.getsockname()[1]


def _visiting(**overrides: Any) -> dict[str, Any]:
    return {
        "robots": lambda url: None,
        "take_capture": _fake_capture,
        "visit": _fake_visit,
        "pause_seconds": 0,
        "per_stratum": 2,
        "log": lambda _: None,
    } | overrides


async def test_a_dropped_connection_is_recorded_as_such_and_tried_on_resume(
    tmp_path: Path,
) -> None:
    files = RunFiles(tmp_path)
    connected = [True]

    def robots(url: str) -> str | None:
        if "c1." in url:
            return "robots.txt disallows this path"
        if not connected[0]:
            raise RobotsUnreachable("robots.txt could not be fetched")
        return None

    async def good_capture(url: str, shot: Path, text: Path, record: CaptureRecord) -> None:
        await _fake_capture(url.replace("c2.", "cc."), shot, text, record)

    candidates = _candidates(count=4)
    connected[0] = False
    settings = _visiting(robots=robots, take_capture=good_capture)
    assert (await visit_candidates(files, candidates, **settings))["top-1k"] == 0
    statuses = {key: r.status for key, r in load_index(files).items()}
    assert statuses == {
        "top-1k/001": "robots",
        "top-1k/002": "unreachable",
        "top-1k/003": "unreachable",
        "top-1k/004": "unreachable",
    }

    connected[0] = True
    assert (await visit_candidates(files, candidates, **settings))["top-1k"] == 2
    statuses = {key: r.status for key, r in load_index(files).items()}
    assert statuses == {
        "top-1k/001": "robots",  # a refusal is final
        "top-1k/002": "captured",
        "top-1k/003": "captured",
        "top-1k/004": "unreachable",  # the stratum had its number, so it was not needed
    }


@pytest.mark.parametrize(
    ("failure", "status"),
    [
        ("navigation to u failed: Page.goto: net::ERR_INTERNET_DISCONNECTED at u", "unreachable"),
        (
            "navigation to u failed: Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at u",
            "unreachable",
        ),
        ("'a.test' does not resolve: getaddrinfo failed", "unreachable"),
        ("'a.test' resolves to 10.0.0.1, which the policy refuses: private", "failed"),
        ("the page was too large", "failed"),
    ],
)
async def test_a_capture_that_lost_the_connection_is_tried_again(
    tmp_path: Path, failure: str, status: str
) -> None:
    async def failing(url: str, shot: Path, text: Path, record: CaptureRecord) -> None:
        del url, shot, text, record
        raise RuntimeError(failure)

    files = RunFiles(tmp_path)
    await visit_candidates(files, _candidates(count=1), **_visiting(take_capture=failing))
    assert load_index(files)["top-1k/001"].status == status
    await visit_candidates(files, _candidates(count=1), **_visiting())
    resumed = load_index(files)["top-1k/001"].status
    assert resumed == ("captured" if status == "unreachable" else status)


async def test_a_wcc_visit_that_lost_the_connection_is_tried_again(tmp_path: Path) -> None:
    async def offline(url: str, record: WccRecord) -> None:
        del url
        record.error = "driver_error: navigation failed: net::ERR_CONNECTION_RESET"

    async def broken_page(url: str, record: WccRecord) -> None:
        del url
        record.error = "limit_exceeded: too many elements"

    files = RunFiles(tmp_path)
    await visit_candidates(files, _candidates(count=1), **_visiting(visit=offline))
    assert load_index(files)["top-1k/001"].status == "unreachable"
    assert load_wcc(files) == {}
    await visit_candidates(files, _candidates(count=1), **_visiting(visit=broken_page))
    record = load_index(files)["top-1k/001"]
    assert (record.status, record.wcc_complete) == ("captured", False)
    assert load_wcc(files)["top-1k/001"].error == "limit_exceeded: too many elements"


@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_the_real_capture_and_visit_call_a_closed_port_unreachable(
    tmp_path: Path, scheme: str
) -> None:
    url = f"{scheme}://127.0.0.1:{_closed_port()}"
    candidates = [Candidate("top-1k", 1, url, 1_000)]

    async def no_capture(url: str, shot: Path, text: Path, record: CaptureRecord) -> None:
        del shot, text
        record.final_url = url

    files = RunFiles(tmp_path / "capture")
    await visit_candidates(files, candidates, **_visiting(take_capture=_private_capture))
    assert load_index(files)["top-1k/001"].status == "unreachable"

    files = RunFiles(tmp_path / "visit")
    settings = _visiting(take_capture=no_capture, visit=_private_visit)
    await visit_candidates(files, candidates, **settings)
    assert load_index(files)["top-1k/001"].status == "unreachable"


def test_a_name_that_does_not_resolve_is_not_a_refusal_by_the_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The proxy reports it through the callback a refused address uses."""
    real = socket.getaddrinfo

    def no_such_name(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host == "nowhere.invalid":
            raise socket.gaierror("no such name")
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", no_such_name)
    for scheme in ("http", "https"):
        with pytest.raises(RobotsUnreachable):
            guarded_robots_refusal(f"{scheme}://nowhere.invalid/")


@pytest.mark.parametrize("answer", [[], "slow"])
def test_a_name_with_no_answer_is_not_a_refusal_by_the_policy(
    monkeypatch: pytest.MonkeyPatch, answer: Any
) -> None:
    async def resolver(host: str, port: int) -> list[str]:
        del host, port
        if answer == "slow":
            await asyncio.sleep(30)
        return []

    proxy = functools.partial(EgressProxy, resolver=resolver, connect_deadline_s=0.2)
    monkeypatch.setattr(run_module, "EgressProxy", proxy)
    with pytest.raises(RobotsUnreachable):
        guarded_robots_refusal("https://nowhere.invalid/")


def test_the_run_says_when_candidates_are_waiting_to_be_tried_again(
    tmp_path: Path, visits: list[int], capsys: pytest.CaptureFixture[str]
) -> None:
    files = RunFiles(tmp_path)
    assert run_all(tmp_path) == 0
    assert "could not be reached" not in capsys.readouterr().out
    run_module._write(files.index, [CaptureRecord("k", "s", 1, "o", "u", "unreachable")])
    assert run_all(tmp_path) == 0
    assert "1 candidates could not be reached" in capsys.readouterr().out
    assert len(visits) == 2


def _with_every_other_candidate(files: RunFiles, status: str) -> None:
    index = json.loads(files.index.read_text(encoding="utf-8"))
    seen = {record["key"] for record in index}
    for candidate in _all_candidates():
        if key_of(candidate) not in seen:
            index.append(
                asdict(
                    CaptureRecord(
                        key_of(candidate),
                        candidate.stratum,
                        candidate.position,
                        candidate.origin,
                        candidate.url,
                        status,
                    )
                )
            )
    files.index.write_text(json.dumps(index), encoding="utf-8")


@pytest.mark.parametrize(
    ("status", "state"), [("robots", "short"), ("failed", "short"), ("unreachable", "unfinished")]
)
def test_two_strata_of_no_pages_are_not_a_pass(tmp_path: Path, status: str, state: str) -> None:
    files = _write_run(tmp_path, {"top-1k": 15, "1k-10k": 15}, offered=_RIGHT)
    _with_every_other_candidate(files, status)
    document = score_files(files, _all_candidates())
    assert set(document["verdict"]["criteria"].values()) == {"pass"}
    assert document["strata"]["10k-100k"] == document["strata"]["100k-1m"] == state
    assert document["verdict"]["overall"] == "not judged"
    assert document["accounting"]["10k-100k"][status] == plan.CANDIDATES_PER_STRATUM


def test_one_stratum_one_page_short_is_not_a_pass(tmp_path: Path) -> None:
    pages = {s.name: 15 for s in plan.STRATA} | {"top-1k": 14}
    files = _write_run(tmp_path, pages, offered=_RIGHT)
    _with_every_other_candidate(files, "robots")
    document = score_files(files, _all_candidates())
    assert document["verdict"]["short_strata"] == ["top-1k"]
    assert document["verdict"]["overall"] == "not judged"
    assert "used every candidate and came up short, so the run cannot pass" in report(document)

    files = _write_run(tmp_path / "all", {s.name: 15 for s in plan.STRATA}, offered=_RIGHT)
    assert score_files(files, _all_candidates())["verdict"]["overall"] == "pass"


# -- the scoring step checks the conditions before it reads anything ----------------


def _in_repo(root: Path, *args: str) -> None:
    identity = ["-c", "user.name=t", "-c", "user.email=t@t.test", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", *identity, *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real git repository standing in for this one. Nothing here touches the real one."""
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    _in_repo(root, "init", "--quiet")
    _in_repo(root, "commit", "--quiet", "--allow-empty", "-m", "start")
    monkeypatch.setattr(run_module, "REPO_ROOT", root)
    monkeypatch.setattr(run_module, "code_sha256", lambda: "code")
    monkeypatch.setattr(score_module, "load_sample", _all_candidates)
    return root


@pytest.fixture
def run_of_record(repo: Path) -> RunFiles:
    """Finished, labelled and committed: half the search labels make T2 fail."""
    out = repo / "benchmarks" / "results" / "heldout-x"
    files = _write_run(out, {s.name: 15 for s in plan.STRATA}, offered=_RIGHT)
    files.meta.write_text(json.dumps(run_metadata()), encoding="utf-8")
    document = json.loads(files.labels.read_text(encoding="utf-8"))
    for i, label in enumerate(document["labels"].values()):
        label["search"] = i % 2 == 0
    files.labels.write_text(json.dumps(document), encoding="utf-8")
    _in_repo(repo, "add", "-A")
    _in_repo(repo, "commit", "--quiet", "-m", "the run and its labels")
    return files


def _assert_stopped(files: RunFiles, capsys: pytest.CaptureFixture[str], reason: str) -> None:
    capsys.readouterr()
    assert score_run(files.out) == 2
    out = capsys.readouterr().out
    assert "not scored" in out and reason in out
    assert not (files.out / "score.json").exists()
    assert not (files.out / "report.md").exists()


def test_a_run_under_its_own_conditions_is_scored_and_names_its_plan(
    run_of_record: RunFiles, capsys: pytest.CaptureFixture[str]
) -> None:
    assert score_run(run_of_record.out) == 0
    document = json.loads((run_of_record.out / "score.json").read_text(encoding="utf-8"))
    assert document["verdict"]["overall"] == "fail"
    assert (
        document["run"]["plan_py_sha256"]
        == hashlib.sha256(Path(plan.__file__).read_bytes()).hexdigest()
    )
    text = capsys.readouterr().out
    for name in ("PLAN.md", "plan.py", "sample.json", "labels.json"):
        assert f"`{name}`: `{document['inputs'][name]}`" in text
    assert document["inputs"]["plan.py"] == document["run"]["plan_py_sha256"]
    assert document["inputs"]["PLAN.md"] == document["run"]["plan_sha256"]
    assert f"commit `{document['run']['commit']}`" in text


def test_a_lowered_threshold_stops_the_score(
    run_of_record: RunFiles,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = Path(plan.__file__).read_text(encoding="utf-8")
    assert "SEARCH_MIN_PRECISION = 0.90" in source
    edited = tmp_path / "plan.py"
    edited.write_text(
        source.replace("SEARCH_MIN_PRECISION = 0.90", "SEARCH_MIN_PRECISION = 0.10"),
        encoding="utf-8",
    )
    monkeypatch.setattr(plan, "__file__", str(edited))
    _assert_stopped(run_of_record, capsys, "plan_py_sha256")


@pytest.mark.parametrize(
    "key", ["code_sha256", "plan_sha256", "plan_py_sha256", "sample_sha256", "commit"]
)
def test_a_fingerprint_that_differs_stops_the_score(
    run_of_record: RunFiles, capsys: pytest.CaptureFixture[str], key: str
) -> None:
    recorded = json.loads(run_of_record.meta.read_text(encoding="utf-8"))
    recorded[key] = "0" * 40
    run_of_record.meta.write_text(json.dumps(recorded), encoding="utf-8")
    _in_repo(run_of_record.out, "add", "-A")
    _in_repo(run_of_record.out, "commit", "--quiet", "-m", "edited")
    _assert_stopped(run_of_record, capsys, key if key != "commit" else "the run started at commit")


def test_a_commit_outside_the_run_stops_the_score(
    run_of_record: RunFiles, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "elsewhere.py").write_text("x = 1\n", encoding="utf-8")
    _in_repo(repo, "add", "-A")
    _in_repo(repo, "commit", "--quiet", "-m", "code")
    _assert_stopped(run_of_record, capsys, "the run started at commit")


def test_a_commit_inside_the_run_does_not_stop_the_score(
    run_of_record: RunFiles, repo: Path
) -> None:
    (run_of_record.out / "note.txt").write_text("note\n", encoding="utf-8")
    _in_repo(repo, "add", "-A")
    _in_repo(repo, "commit", "--quiet", "-m", "note")
    assert score_run(run_of_record.out) == 0


def test_no_run_file_stops_the_score(
    run_of_record: RunFiles, capsys: pytest.CaptureFixture[str]
) -> None:
    run_of_record.meta.unlink()
    _assert_stopped(run_of_record, capsys, "run.json is missing")


def test_an_unreadable_run_file_stops_the_score(
    run_of_record: RunFiles, capsys: pytest.CaptureFixture[str]
) -> None:
    run_of_record.meta.write_text("{", encoding="utf-8")
    _assert_stopped(run_of_record, capsys, "cannot be read")


@pytest.mark.parametrize("state", ["edited", "staged", "untracked"])
def test_labels_not_committed_as_they_stand_stop_the_score(
    run_of_record: RunFiles, repo: Path, capsys: pytest.CaptureFixture[str], state: str
) -> None:
    labels = run_of_record.labels
    if state == "untracked":
        _in_repo(repo, "rm", "--quiet", "--cached", str(labels))
        _in_repo(repo, "commit", "--quiet", "-m", "labels out")
    else:
        labels.write_text(labels.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        if state == "staged":
            _in_repo(repo, "add", "-A")
    _assert_stopped(run_of_record, capsys, "committed")


def test_second_labels_not_committed_stop_the_score(
    run_of_record: RunFiles, capsys: pytest.CaptureFixture[str]
) -> None:
    second = run_of_record.out / "labels-second.json"
    second.write_text(json.dumps({"annotator": "b", "labels": {}}), encoding="utf-8")
    _assert_stopped(run_of_record, capsys, "labels-second.json is not committed")


def test_a_run_outside_the_repository_cannot_be_scored(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    del repo
    files = _write_run(tmp_path / "elsewhere", {"top-1k": 1}, offered=_RIGHT)
    files.meta.write_text(json.dumps(run_metadata()), encoding="utf-8")
    _assert_stopped(files, capsys, "outside the repository")


# -- limits on one candidate --------------------------------------------------------


def test_an_unusable_redirect_on_robots_is_a_failed_candidate(
    serve: Callable[[Routes], str], tmp_path: Path
) -> None:
    base = serve({"/robots.txt": (302, {"Location": "http://[bad/x"}, b"")})
    good = serve({"/robots.txt": (404, {}, b"")})
    candidates = [Candidate("top-1k", 1, base, 1_000), Candidate("top-1k", 2, good, 1_000)]

    async def run() -> None:
        await visit_candidates(
            RunFiles(tmp_path),
            candidates,
            **_visiting(
                robots=lambda url: guarded_robots_refusal(url, allow_private_network=True),
                per_stratum=1,
                landing=_one_host,
            ),
        )

    asyncio.run(run())
    index = load_index(RunFiles(tmp_path))
    assert index["top-1k/001"].status == "failed"
    assert "unusable URL" in index["top-1k/001"].reason
    assert index["top-1k/002"].status == "captured"


def test_only_the_start_of_a_huge_robots_file_is_read(serve: Callable[[Routes], str]) -> None:
    past_the_limit = b"#" * run_module.ROBOTS_MAX_BYTES + b"\nDisallow: /\n"
    base = serve({"/robots.txt": (200, {}, b"User-agent: *\n" + past_the_limit)})
    assert guarded_robots_refusal(f"{base}/", allow_private_network=True) is None
    within = serve({"/robots.txt": (200, {}, b"User-agent: *\nDisallow: /\n#" + past_the_limit)})
    assert guarded_robots_refusal(f"{within}/", allow_private_network=True) is not None


@pytest.mark.parametrize("stage", ["head", "body"])
def test_a_robots_file_sent_a_byte_at_a_time_runs_out_of_time(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """Each byte resets the socket's timeout, so only a deadline on the whole read ends it."""
    monkeypatch.setattr(run_module, "ROBOTS_DEADLINE_SECONDS", 1.0)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def drip() -> None:
        with contextlib.suppress(OSError):
            connection, _ = listener.accept()
            with connection:
                connection.recv(65_536)
                connection.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                if stage == "body":
                    connection.sendall(b"yes\r\n\r\n")
                while True:
                    connection.sendall(b"a")
                    time.sleep(0.05)

    threading.Thread(target=drip, daemon=True).start()
    started = time.monotonic()
    try:
        with pytest.raises(RobotsUnreachable, match="not read within"):
            guarded_robots_refusal(
                f"http://127.0.0.1:{listener.getsockname()[1]}/", allow_private_network=True
            )
    finally:
        listener.close()
    assert time.monotonic() - started < 10


async def _never(*args: Any) -> None:
    del args
    await asyncio.sleep(3_600)


async def test_a_capture_that_never_returns_costs_one_candidate(tmp_path: Path) -> None:
    async def hangs_on_the_first(url: str, shot: Path, text: Path, record: CaptureRecord) -> None:
        if "c1." in url:
            await _never()
        await _fake_capture(url, shot, text, record)

    files = RunFiles(tmp_path)
    settings = _visiting(take_capture=hangs_on_the_first, candidate_timeout=0.2, per_stratum=1)
    assert (await visit_candidates(files, _candidates(count=3), **settings))["top-1k"] == 1
    index = load_index(files)
    assert index["top-1k/001"].status == "failed"
    assert index["top-1k/001"].reason == "timed out after 0.2 s during the capture"
    assert index["top-1k/003"].status == "captured"


async def test_a_wcc_visit_that_never_returns_is_recorded_and_not_counted(tmp_path: Path) -> None:
    files = RunFiles(tmp_path)
    settings = _visiting(visit=_never, candidate_timeout=0.2, per_stratum=1)
    assert (await visit_candidates(files, _candidates(count=1), **settings))["top-1k"] == 0
    assert load_index(files)["top-1k/001"].status == "captured"
    assert load_index(files)["top-1k/001"].wcc_complete is False
    assert load_wcc(files)["top-1k/001"].error == "timed out after 0.2 s during the WCC visit"


async def test_a_robots_read_that_never_returns_costs_one_candidate(tmp_path: Path) -> None:
    release = threading.Event()
    try:
        settings = _visiting(robots=lambda url: release.wait(5), candidate_timeout=0.2)
        await visit_candidates(RunFiles(tmp_path), _candidates(count=1), **settings)
    finally:
        release.set()
    record = load_index(RunFiles(tmp_path))["top-1k/001"]
    assert (record.status, record.reason) == ("failed", "timed out after 0.2 s during robots.txt")


_HUNG = b"<!doctype html><title>h</title><p>h</p><script>setTimeout(()=>{while(1){}},50)</script>"


@pytest.mark.parametrize("phase", ["the capture", "the WCC visit"])
async def test_a_page_that_hangs_the_browser_is_cut_off_and_the_run_goes_on(
    serve: Callable[[Routes], str], tmp_path: Path, phase: str
) -> None:
    base = serve({"/": (200, {"Content-Type": "text/html"}, _HUNG)})
    candidates = [Candidate("top-1k", 1, base, 1_000)]
    files = RunFiles(tmp_path)
    settings = (
        _visiting(take_capture=_private_capture)
        if phase == "the capture"
        else _visiting(visit=_private_visit)
    )
    started = time.monotonic()
    await visit_candidates(files, candidates, **settings, candidate_timeout=6)
    assert time.monotonic() - started < 40
    record = load_index(files)["top-1k/001"]
    if phase == "the capture":
        assert record.status == "failed"
        assert record.reason == "timed out after 6 s during the capture"
    else:
        assert load_wcc(files)["top-1k/001"].error == "timed out after 6 s during the WCC visit"


async def test_a_browser_that_will_not_close_does_not_hold_up_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Stuck:
        shutdown = staticmethod(_never)

    monkeypatch.setattr(run_module, "SHUTDOWN_TIMEOUT_SECONDS", 0.05)
    await asyncio.wait_for(_shut_down(Stuck()), 5)  # type: ignore[arg-type]


# -- labels are compared with what a person can see ---------------------------------


_VISIBLE_AND_NAMED = (
    b"<!doctype html><title>t</title><main>"
    b"<form action=/search role=search><input type=search name=q aria-label=Search>"
    b'<button aria-label="Run a query">Go</button></form>'
    b"<ul>"
    + b"".join(
        f'<li class=card><a href="/j{i}" aria-label="Open offer {i}">Join now {i}</a>'
        f"<p>description {i}</p></li>".encode()
        for i in range(6)
    )
    + b"</ul></main>"
)


async def test_visible_text_is_recorded_and_compared_and_the_name_is_kept(
    serve: Callable[[Routes], str],
) -> None:
    base = serve({"/": (200, {"Content-Type": "text/html"}, _VISIBLE_AND_NAMED)})
    record = WccRecord("k", f"{base}/")
    await wcc_visit(f"{base}/", record, allow_private_network=True)
    assert record.error is None and record.visible_text_unread == 0
    assert record.result_list[:2] == ["Join now 0", "Join now 1"]
    assert record.result_list_names[:2] == ["Open offer 0", "Open offer 1"]
    by_name = {o["name"]: o for o in record.offered}
    assert by_name["submit_search_form"]["labels"] == ["Go"]
    assert by_name["submit_search_form"]["names"] == ["Search", "Run a query"]
    assert by_name["open_result_item"]["labels"][0] == "Join now 0"
    assert by_name["open_result_item"]["names"][0] == "Open offer 0"

    label = _label(
        search=True,
        result_list=True,
        titles=["Join now 0", "Join now 1"],
        controls=["Join now 0"],
    )
    page = score_page("k", "s", "o", label, record)
    assert page.results_matched
    assert page.unsafe == ["open_result_item: Join now 0"]
    hidden_name = _label(result_list=True, titles=["Open offer 0", "Open offer 1"])
    assert not score_page("k", "s", "o", hidden_name, record).results_matched


_SEARCH_BOX = "<input type=search name=q aria-label=Q>"


@pytest.mark.parametrize(
    ("form", "visible"),
    [
        (f"{_SEARCH_BOX}<input type=submit value=Join>", "Join"),
        (f"{_SEARCH_BOX}<button><span style='display:none'>Hidden </span>Shown</button>", "Shown"),
        (
            "<label for=q>Your words</label><input id=q type=search name=q><button>Go</button>",
            "Your words",
        ),
        ("<input type=search name=q placeholder=Words><button>Go</button>", "Words"),
        (
            f"{_SEARCH_BOX}<label for=b>Sort</label><select id=b name=b><option>x<option>y</select>"
            "<button>Go</button>",
            "Sort",
        ),
    ],
)
async def test_the_visible_text_of_each_kind_of_control(
    serve: Callable[[Routes], str], form: str, visible: str
) -> None:
    page = f"<!doctype html><title>t</title><form role=search action=/s>{form}</form>"
    base = serve({"/": (200, {"Content-Type": "text/html"}, page.encode())})
    record = WccRecord("k", f"{base}/")
    await wcc_visit(f"{base}/", record, allow_private_network=True)
    assert record.error is None
    labels = {label for o in record.offered for label in o["labels"]}
    assert visible in labels, record.offered
    assert "Hidden" not in " ".join(labels)
    assert "x" not in labels


def test_the_report_counts_elements_whose_name_stood_in(tmp_path: Path) -> None:
    files = _write_run(tmp_path, {"top-1k": 2}, offered=_RIGHT)
    results = json.loads(files.wcc.read_text(encoding="utf-8"))
    results[0]["visible_text_unread"] = 3
    files.wcc.write_text(json.dumps(results), encoding="utf-8")
    document = score_files(files, _all_candidates())
    assert document["visible_text_unread"] == 3
    assert "with their name used instead: 3" in report(document)


def test_the_report_prints_every_label_and_the_names_that_differ(tmp_path: Path) -> None:
    labels = ["One", "Two", "Three", "Four", "Five"]
    offered = [
        {"name": "x", "category": "submit", "risk": "low", "labels": labels, "names": ["Aria"]}
    ]
    files = _write_run(tmp_path, {"top-1k": 1}, offered=offered)
    text = report(score_files(files, _all_candidates()))
    assert "x [low] (One, Two, Three, Four, Five) named Aria" in text


# -- the landing address is judged too ----------------------------------------------


def test_the_landing_rules() -> None:
    assert landing_exclusion("https://shop.test/", {}) is None
    assert landing_exclusion("", {}) is None
    assert "E2" in (landing_exclusion("https://docs.python.org/x", {}) or "")
    assert "E2" in (landing_exclusion("https://WWW.Wikipedia.ORG/", {}) or "")
    assert "E4" in (landing_exclusion("https://m.a.test/", {"a.test": "k"}) or "")
    assert "E4" in (landing_exclusion("https://a.test/", {"a.test": "k"}) or "")
    assert landing_exclusion("https://b.a.test/", {"a.test": "k"}) is None


def _landing_on(where: dict[str, str]) -> Any:
    async def lands(url: str, shot: Path, text: Path, record: CaptureRecord) -> None:
        await _fake_capture(url.replace("c2.", "cc."), shot, text, record)
        for needle, final in where.items():
            if needle in url:
                record.final_url = final

    return lands


async def test_a_redirect_to_a_development_site_is_replaced_before_wcc_visits(
    tmp_path: Path,
) -> None:
    visited: list[str] = []

    async def visit(url: str, record: WccRecord) -> None:
        visited.append(url)
        record.final_url = url

    files = RunFiles(tmp_path)
    capture_it = _landing_on({"c1.": "https://docs.python.org/3/"})
    settings = _visiting(take_capture=capture_it, visit=visit, per_stratum=1)
    assert (await visit_candidates(files, _candidates(count=2), **settings))["top-1k"] == 1
    record = load_index(files)["top-1k/001"]
    assert record.status == "landed_elsewhere" and record.reason.startswith("E2")
    assert visited == ["https://c2.test/"]


async def test_a_redirect_to_a_site_already_kept_is_replaced(tmp_path: Path) -> None:
    files = RunFiles(tmp_path)
    capture_it = _landing_on(
        {
            "c1.": "https://www.shared.test/",
            "c2.": "https://m.shared.test/",
            "c3.": "https://c1.test/",
        }
    )
    settings = _visiting(take_capture=capture_it, per_stratum=2)
    assert (await visit_candidates(files, _candidates(count=4), **settings))["top-1k"] == 2
    index = load_index(files)
    assert index["top-1k/001"].status == "captured"
    for key in ("top-1k/002", "top-1k/003"):  # by where the first ended, and started
        assert index[key].status == "landed_elsewhere"
        assert index[key].reason.endswith("the site of top-1k/001")
    assert index["top-1k/004"].status == "captured"


async def test_a_page_that_failed_keeps_no_site_for_itself(tmp_path: Path) -> None:
    async def broken(url: str, record: WccRecord) -> None:
        record.final_url = url
        record.error = "limit_exceeded: too many elements" if "c1." in url else None

    files = RunFiles(tmp_path)
    capture_it = _landing_on({"c3.": "https://c1.test/"})
    settings = _visiting(take_capture=capture_it, visit=broken, per_stratum=2)
    await visit_candidates(files, [c for c in _candidates(count=3) if c.position != 2], **settings)
    assert load_index(files)["top-1k/003"].status == "captured"


async def test_where_wcc_landed_is_judged_too(tmp_path: Path) -> None:
    async def visit(url: str, record: WccRecord) -> None:
        record.final_url = "https://docs.python.org/" if "c1." in url else url

    files = RunFiles(tmp_path)
    settings = _visiting(take_capture=_landing_on({}), visit=visit, per_stratum=1)
    assert (await visit_candidates(files, _candidates(count=2), **settings))["top-1k"] == 1
    record = load_index(files)["top-1k/001"]
    assert record.status == "landed_elsewhere" and "E2" in record.reason
    assert load_index(files)["top-1k/002"].status == "captured"


def test_a_page_that_landed_elsewhere_is_counted_as_used_and_not_scored() -> None:
    candidates = _candidates(count=2)
    index = {
        key_of(c): CaptureRecord(key_of(c), c.stratum, c.position, c.origin, c.url, "captured")
        for c in candidates
    }
    index["top-1k/001"].status = "landed_elsewhere"
    results = {key: WccRecord(key, "u") for key in index}
    scored, tally = select_scored(candidates, index, results, {"top-1k/002": _label()}, 1)
    assert [key_of(c) for c in scored] == ["top-1k/002"]
    assert tally["top-1k"]["landed_elsewhere"] == 1 and tally["top-1k"]["used"] == 2


# -- the second annotator -----------------------------------------------------------


def _labelled_run(tmp_path: Path) -> RunFiles:
    """Six pages: 001 no WCC result, 002 excluded, 003-005 scored, 006 past the window."""
    files = RunFiles(tmp_path)
    files.captures.mkdir(parents=True)
    records, labels = [], {}
    for position in range(1, 7):
        key = f"top-1k/{position:03d}"
        record = CaptureRecord(
            key, "top-1k", position, f"https://c{position}.test", "u", "captured"
        )
        record.wcc_complete = position != 1
        record.screenshot, record.text = f"{position}.png", f"{position}.txt"
        records.append(asdict(record))
        labels[key] = _label(eligible=position != 2)
    files.index.write_text(json.dumps(records), encoding="utf-8")
    files.labels.write_text(json.dumps({"annotator": "First", "labels": labels}), encoding="utf-8")
    # A directory where the results file would be: reading it would raise.
    files.wcc.mkdir(parents=True)
    return files


_THREE_PAGES = ["top-1k/003", "top-1k/004", "top-1k/005"]


def test_the_second_annotator_labels_only_scored_pages(tmp_path: Path) -> None:
    files = _labelled_run(tmp_path)
    shown: list[str] = []
    path = label_pages(
        files,
        "Second",
        second=True,
        ask=_scripted(*["y", "n", "n", "n", "n"] * 3),
        say=shown.append,
        per_stratum=3,
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    assert sorted(document["labels"]) == document["pages"] == _THREE_PAGES
    assert not any("top-1k/006" in line or "top-1k/001" in line for line in shown)


def test_the_pages_for_the_second_annotator_are_the_scores_own(tmp_path: Path) -> None:
    files = _labelled_run(tmp_path)
    index = load_index(files)
    first = json.loads(files.labels.read_text(encoding="utf-8"))["labels"]
    candidates = [Candidate("top-1k", r.position, r.origin, 1_000) for r in index.values()]
    results = {
        k: WccRecord(k, "u", error=None if r.wcc_complete else "x") for k, r in index.items()
    }
    scored, _ = select_scored(candidates, index, results, first, 3)
    assert scored_keys(index, first, 3) == [key_of(c) for c in scored] == _THREE_PAGES


def test_the_pages_drawn_stay_drawn_when_labelling_resumes(tmp_path: Path) -> None:
    files = _labelled_run(tmp_path)
    second = files.out / "labels-second.json"
    second.write_text(
        json.dumps({"annotator": "Second", "pages": ["top-1k/006"], "labels": {}}),
        encoding="utf-8",
    )
    label_pages(
        files, "Second", second=True, ask=_scripted("y", "n", "n", "n", "n"), say=lambda _: None
    )
    assert list(json.loads(second.read_text(encoding="utf-8"))["labels"]) == ["top-1k/006"]


@pytest.mark.parametrize("name", ["First", " first "])
def test_the_second_annotator_cannot_be_the_first(tmp_path: Path, name: str) -> None:
    files = _labelled_run(tmp_path)
    with pytest.raises(SystemExit, match="second person"):
        label_pages(files, name, second=True, ask=_scripted(), say=lambda _: None)
    assert not (files.out / "labels-second.json").exists()


def test_the_second_annotator_waits_for_the_first(tmp_path: Path) -> None:
    files = _labelled_run(tmp_path)
    document = json.loads(files.labels.read_text(encoding="utf-8"))
    del document["labels"]["top-1k/004"]
    files.labels.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SystemExit, match="has not finished"):
        label_pages(files, "Second", second=True, ask=_scripted(), say=lambda _: None)

    files.labels.unlink()
    with pytest.raises(SystemExit, match="labels first"):
        label_pages(files, "Second", second=True, ask=_scripted(), say=lambda _: None)


# -- the command line, end to end, on loopback ---------------------------------------


def test_the_command_line_takes_a_run_from_visits_to_a_report(
    fixture_server: str,
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``run``, ``label``, ``score``, with the visits pointed at this machine.

    Only three things are replaced: the sample, because the real one is the web; the
    private-network allowance, because the real one refuses loopback; and the number
    of pages. The real robots read, capture, WCC visit, labelling prompts, git checks
    and report all run.
    """
    candidates = _fixture_candidates(fixture_server)
    real_visits = run_module.visit_candidates

    async def on_loopback(files: RunFiles, chosen: list[Candidate]) -> dict[str, int]:
        return await real_visits(
            files,
            chosen,
            robots=lambda url: guarded_robots_refusal(url, allow_private_network=True),
            take_capture=_private_capture,
            visit=_private_visit,
            pause_seconds=0,
            per_stratum=3,
            log=lambda _: None,
            landing=_one_host,
        )

    monkeypatch.setattr(run_module, "visit_candidates", on_loopback)
    monkeypatch.setattr(run_module, "load_sample", lambda: candidates)
    monkeypatch.setattr(score_module, "load_sample", lambda: candidates)

    out = repo / "benchmarks" / "results" / "heldout-cli"
    arguments = ["--out", str(out)]
    assert main(["run", *arguments]) == 0
    assert "top-1k: 3 of 15 complete pages  (short)" in capsys.readouterr().out
    assert sorted(r.status for r in load_index(RunFiles(out)).values()) == ["captured"] * 3

    answers = [
        *("y", "y", "n", "n", "n"),
        *("y", "n", "y", "Wireless Earbuds", "Bluetooth Speaker", "y", "y", "Add to cart", ""),
        *("y", "y", "n", "n", "n"),
    ]
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(answers) + "\n"))
    assert main(["label", *arguments, "--annotator", "tester"]) == 0
    with pytest.raises(SystemExit, match="second person"):
        main(["label", *arguments, "--annotator", "tester", "--second"])
    capsys.readouterr()

    # The labels are not committed yet, so WCC's results stay unread.
    assert main(["score", *arguments]) == 2
    assert "labels.json is not committed" in capsys.readouterr().out
    assert not (out / "report.md").exists()

    _in_repo(repo, "add", str(out / "labels.json"))
    _in_repo(repo, "commit", "--quiet", "-m", "labels")
    assert main(["score", *arguments]) == 0
    text = capsys.readouterr().out
    assert "**Verdict: not judged** over 3 scored pages" in text
    assert "not finished, so the run cannot pass" in text
    document = json.loads((out / "score.json").read_text(encoding="utf-8"))
    _assert_the_fixtures_score_as_labelled(document)
    assert (out / "report.md").read_text(encoding="utf-8") == text.rstrip("\n") + "\n"
    assert f"`plan.py`: `{document['run']['plan_py_sha256']}`" in text

    # The same run, with the line moved afterwards, is no longer scored.
    edited = tmp_path / "plan.py"
    edited.write_text("SEARCH_MIN_PRECISION = 0.10\n", encoding="utf-8")
    monkeypatch.setattr(plan, "__file__", str(edited))
    assert main(["score", *arguments]) == 2
    assert "plan_py_sha256" in capsys.readouterr().out
