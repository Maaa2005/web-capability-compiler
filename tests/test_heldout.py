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
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from benchmarks.heldout import plan
from benchmarks.heldout import run as run_module
from benchmarks.heldout.__main__ import _printing_never_fails
from benchmarks.heldout.label import agreement_keys, ask_page, label_pages
from benchmarks.heldout.run import (
    REPO_ROOT,
    CaptureRecord,
    RunFiles,
    WccRecord,
    _commit_mismatch,
    capture,
    code_sha256,
    free_text_parameters,
    guarded_robots_refusal,
    key_of,
    run_all,
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
    report,
    score_files,
    score_page,
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
    answer = guarded_robots_refusal(
        f"https://127.0.0.2:{internal.port}/", allow_private_network=True
    )
    assert answer is not None and "disallows" in answer
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
    answer = guarded_robots_refusal(f"http://127.0.0.1:{port}/", allow_private_network=True)
    assert answer is not None and robots_refusal(f"http://127.0.0.1:{port}/") is not None


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
