"""Visit the held-out candidates: ``robots.txt``, a capture, then WCC.

Each candidate is visited once, in the order ``sample.json`` gives, until each
stratum has the plan's number of complete pages. The run can be stopped and
resumed: everything is written after each candidate. A candidate the connection
failed on says nothing about the site, so a resumed run tries it again.

Two files, kept apart on purpose:

- ``captures/index.json`` (with the screenshots and text beside it) is what the
  annotator reads.
- ``wcc/results.json`` is what WCC said. The labelling step never opens it, and
  ``PLAN.md`` asks the annotator not to either.

Screenshots and page text belong to the sites and stay out of git
(``.gitignore``); the index records each file's SHA-256 so a later reader can
tell whether a capture was replaced.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import platform
import ssl
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from http.client import HTTPException, InvalidURL
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    BaseHandler,
    HTTPDefaultErrorHandler,
    HTTPErrorProcessor,
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    OpenerDirector,
    Request,
    UnknownHandler,
)
from urllib.robotparser import RobotFileParser

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Response

from benchmarks.heldout import plan
from benchmarks.heldout.sample import (
    SAMPLE_PATH,
    Candidate,
    landing_exclusion,
    load_sample,
    site_key,
)
from benchmarks.measure_coverage import PAUSE_SECONDS, ROBOTS_TIMEOUT_SECONDS, USER_AGENT
from wcc.compiler.navigation import find_result_links
from wcc.driver.base import DriverError, DriverSettings, ElementNotResolvedError
from wcc.driver.egress_proxy import EgressProxy
from wcc.driver.playwright_driver import PlaywrightDriver, _Session
from wcc.enrich.enricher import Enricher
from wcc.limits import ResourceLimits
from wcc.models.capability import RiskLevel
from wcc.models.element import NormalizedElement
from wcc.models.session import OpenSessionRequest
from wcc.policy.url_policy import UrlPolicy
from wcc.runtime.coordinator import Coordinator
from wcc.store.store import Store

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The capture is cut here so one endless page cannot fill the disk.
MAX_SCREENSHOT_HEIGHT = 8_000
MAX_TEXT_CHARS = 50_000
NETWORK_QUIET_MS = 3_000
#: RFC 9309 section 2.5 asks a crawler to parse at least 500 KiB. What is past this
#: is not read; what came before it is parsed, as that section allows.
ROBOTS_MAX_BYTES = 512 * 1024
#: The whole read, however many redirects. The socket's timeout only bounds the
#: silence between two packets, so a server sending a byte at a time never meets it.
ROBOTS_DEADLINE_SECONDS = 40.0
#: One candidate, from ``robots.txt`` to the end of the WCC visit. Far above what an
#: ordinary page takes, because hitting it replaces the page.
CANDIDATE_TIMEOUT_SECONDS = 180.0
#: A browser that will not close must not hold up the rest of the run.
SHUTDOWN_TIMEOUT_SECONDS = 30.0
MAX_LABEL_CHARS = 4_096

#: The words WCC's egress proxy and Chromium use for a connection that failed. Each
#: is a statement about the network: the proxy reports a name that does not resolve
#: through the same callback as a refused address, and neither this list nor anyone
#: reading the record should take that for a refusal. Tests hold the proxy's wording.
CONNECTION_FAILURES: tuple[str, ...] = (
    "does not resolve",
    "resolved to no addresses",
    "did not finish resolving and connecting",
    "net::ERR_INTERNET_DISCONNECTED",
    "net::ERR_NETWORK_",
    "net::ERR_ADDRESS_",
    "net::ERR_CONNECTION_",
    "net::ERR_TUNNEL_CONNECTION_FAILED",
    "net::ERR_PROXY_CONNECTION_FAILED",
    "net::ERR_NAME_",
    "net::ERR_DNS_",
    "net::ERR_EMPTY_RESPONSE",
    "net::ERR_TIMED_OUT",
    "net::ERR_SOCKET_",
)

#: The statuses a resumed run tries again.
RETRIED_ON_RESUME = frozenset({"unreachable"})


class RobotsUnreachable(Exception):
    """``robots.txt`` is unread because the connection failed, so no page is opened.

    An exception rather than a refusal string: the file still disallows for now, as
    RFC 9309 section 2.3.1 says, but the site has not refused anything, and a run
    that records this as a refusal never asks again.
    """


def is_connection_failure(message: str) -> bool:
    return any(words in message for words in CONNECTION_FAILURES)


def key_of(candidate: Candidate) -> str:
    return f"{candidate.stratum}/{candidate.position:03d}"


@dataclass
class CaptureRecord:
    key: str
    stratum: str
    position: int
    origin: str
    url: str
    status: str = "pending"
    """``captured``; ``robots`` (refused by robots.txt or by the URL policy on the way
    to it); ``unreachable`` (the connection failed, so it is tried again on resume);
    ``landed_elsewhere`` (the page it ended on is out by E2 or E4); or ``failed``."""

    reason: str = ""
    final_url: str = ""
    http_status: int | None = None
    title: str = ""
    screenshot: str = ""
    screenshot_sha256: str = ""
    text: str = ""
    text_sha256: str = ""
    captured_at: str = ""
    wcc_complete: bool = False
    """Whether the WCC visit finished. All the second annotator's selection needs to
    know about WCC, kept here so that labelling never opens the results."""


@dataclass
class WccRecord:
    key: str
    url: str
    observed_at: str = ""
    error: str | None = None
    final_url: str = ""
    interactive_elements: int = 0
    forms_seen: int = 0
    offered: list[dict[str, Any]] = field(default_factory=list)
    """Each bound capability: ``name``, ``category``, ``text_parameters`` (free-text
    arguments), the visible ``labels`` of the elements its evidence names, which the
    unsafe-exposure check reads, and their accessible ``names``, which it does not."""

    result_list: list[str] = field(default_factory=list)
    """The visible text of each link in the list WCC took, which titles are matched to."""

    result_list_names: list[str] = field(default_factory=list)
    """The same links' accessible names. An ``aria-label`` takes priority over the text
    there, and an annotator cannot see one."""

    visible_text_unread: int = 0
    """Elements WCC could not find again to read their text. Their name stands in for
    it in ``labels`` and ``result_list``, so an exposure is not missed by not looking."""

    unbound: list[str] = field(default_factory=list)
    refused_codes: list[str] = field(default_factory=list)
    refused_above_low: int = 0

    @property
    def complete(self) -> bool:
        return self.error is None


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _CaptureDriver(PlaywrightDriver):
    """WCC's driver, with the two things a capture needs that its interface lacks.

    A subclass rather than a second browser launch: the capture then has the same
    boundary as the WCC visit, which is the egress proxy (WebSockets, Workers, and
    no second DNS answer), the refused popups and the refused downloads.
    """

    document_status: int | None = None

    async def _arm_observers(self, session: _Session) -> None:
        await super()._arm_observers(session)

        def remember(response: Response) -> None:
            if (
                response.request.is_navigation_request()
                and response.frame == session.page.main_frame
            ):
                self.document_status = response.status

        session.page.on("response", remember)

    async def save_screenshot(self, session_id: str, path: Path) -> None:
        page = self._session(session_id).page
        height = await page.evaluate("document.documentElement.scrollHeight")
        clip_height = max(1, min(int(height or 900), MAX_SCREENSHOT_HEIGHT))
        await page.screenshot(
            path=str(path),
            full_page=True,
            clip={"x": 0, "y": 0, "width": 1280, "height": clip_height},
        )


#: What a person sees of an element: its rendered text, the words on an input
#: button, or for a field the label beside it or else its placeholder.
_VISIBLE_TEXT = """(element) => {
  if (!element.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) return '';
  const type = (element.getAttribute('type') || '').toLowerCase();
  if (element.tagName === 'INPUT' && ['submit', 'button', 'reset'].includes(type)) {
    return element.value || '';
  }
  const field = element.tagName === 'SELECT' || element.tagName === 'TEXTAREA';
  const own = field ? '' : (element.innerText || '').trim();
  if (own) return own;
  const labels = Array.from(element.labels || []).map((label) => label.innerText || '');
  return labels.join(' ').trim() || element.getAttribute('placeholder') || '';
}"""


class _VisibleTextDriver(PlaywrightDriver):
    """WCC's driver, plus the one reading the plan compares with and the IR does not keep."""

    async def visible_text(self, session_id: str, element_id: str) -> str | None:
        """The element's visible text, or ``None`` if it cannot be found again."""
        session = self._session(session_id)
        try:
            handle_id = await self.resolve(session_id, element_id)
            try:
                text = await session.page.evaluate(_VISIBLE_TEXT, self._handle(session, handle_id))
            finally:
                self.release(session_id, handle_id)
        except (ElementNotResolvedError, PlaywrightError):
            return None
        return " ".join(str(text).split())[:MAX_LABEL_CHARS]


async def _shut_down(driver: PlaywrightDriver) -> None:
    """Close the browser, within limits: this also runs when a candidate is cut off."""
    try:
        await asyncio.wait_for(driver.shutdown(), SHUTDOWN_TIMEOUT_SECONDS)
    except TimeoutError:
        print(f"  the browser did not close within {SHUTDOWN_TIMEOUT_SECONDS:g} s; going on")


async def capture(
    url: str,
    shot: Path,
    text: Path,
    record: CaptureRecord,
    *,
    allow_private_network: bool = False,
) -> None:
    """Open ``url`` through WCC's driver and keep what a person would see.

    The page's traffic passes the driver's egress proxy, so the capture cannot reach
    an address WCC itself would refuse. ``allow_private_network`` exists for the
    loopback fixtures in the tests, as it does for WCC.
    """
    driver = _CaptureDriver(
        UrlPolicy(),
        DriverSettings(
            allow_private_network=allow_private_network,
            limits=ResourceLimits(settle_timeout_ms=NETWORK_QUIET_MS),
        ),
    )
    try:
        # Waits for the load event, then up to NETWORK_QUIET_MS for quiet. A page
        # that never goes quiet is captured as it is.
        session = await driver.open_session(OpenSessionRequest(url=url))
        identity = await driver.identify(session.session_id)
        final_url, visible = await driver.page_text(session.session_id)
        await driver.save_screenshot(session.session_id, shot)
        record.http_status = driver.document_status
        record.final_url = final_url
        record.title = identity.title or ""
        text.write_text(visible[:MAX_TEXT_CHARS], encoding="utf-8")
    finally:
        await _shut_down(driver)


class _ThroughProxy(BaseHandler):
    """Send every request to the egress proxy.

    Not ``urllib``'s ``ProxyHandler``: that one goes direct for any host ``no_proxy``
    or the Windows registry's proxy override names, and direct is what must not
    happen here.
    """

    handler_order = 100

    def __init__(self, proxy_url: str) -> None:
        self._proxy = urlsplit(proxy_url).netloc

    def http_open(self, request: Request) -> None:
        request.set_proxy(self._proxy, "http")

    def https_open(self, request: Request) -> None:
        request.set_proxy(self._proxy, "http")


def _read_through(proxy_url: str, url: str) -> bytes:
    """Read ``url`` over HTTP(S) only, every redirect hop passing the proxy.

    Built by hand because ``build_opener`` also adds the proxy lookup from the
    environment and the ``ftp`` and ``file`` handlers, none of which the proxy sees.
    A redirect to one of those schemes ends at ``UnknownHandler`` as an error.
    """
    opener = OpenerDirector()
    for handler in (
        _ThroughProxy(proxy_url),
        HTTPHandler(),
        HTTPSHandler(),
        HTTPRedirectHandler(),
        HTTPDefaultErrorHandler(),
        HTTPErrorProcessor(),
        UnknownHandler(),
    ):
        opener.add_handler(handler)
    with opener.open(url, timeout=ROBOTS_TIMEOUT_SECONDS) as response:
        return response.read(ROBOTS_MAX_BYTES)


def guarded_robots_refusal(url: str, *, allow_private_network: bool = False) -> str | None:
    """``measure_coverage.robots_refusal``'s answers, behind the egress proxy.

    That function reads ``robots.txt`` with a bare ``urlopen``, which follows a
    redirect anywhere and so reaches an internal address before the page's own
    safety check runs. Here every hop is resolved, judged and connected to by the
    same proxy the browser uses. A refused hop leaves the file unread, so it
    disallows, as RFC 9309 section 2.3.1 says of an unreachable file.

    The status rules are copied because that function takes no way to pass a
    connection; a test holds the two to the same answers. It differs in one way: a
    connection that failed raises :class:`RobotsUnreachable` instead of returning a
    refusal, because the site has not refused anything, and a redirect to a URL that
    cannot be parsed raises ``ValueError``. Neither lets a page be opened. Opens its
    own event loop, so call it from a worker thread.
    """
    parts = urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    refused: list[str] = []

    async def fetch() -> bytes:
        proxy = EgressProxy(
            UrlPolicy(),
            allow_private_network=lambda: allow_private_network,
            on_refused=lambda _target, reason: refused.append(reason),
        )
        proxy_url = await proxy.start()
        try:
            # Closing the proxy cuts the worker's connection, which is what ends a
            # read the deadline gave up on. A thread cannot be cancelled.
            return await asyncio.wait_for(
                asyncio.to_thread(_read_through, proxy_url, robots_url), ROBOTS_DEADLINE_SECONDS
            )
        except TimeoutError as late:
            raise RobotsUnreachable(
                f"robots.txt was not read within {ROBOTS_DEADLINE_SECONDS:g} s"
            ) from late
        finally:
            await proxy.close()

    try:
        raw = asyncio.run(fetch())
    except HTTPError as status:
        if status.code in (401, 403):
            return f"robots.txt answered {status.code}, which disallows everything"
        if 400 <= status.code < 500:
            return None
        return f"robots.txt answered {status.code}, so it is unreachable and disallows"
    except (ValueError, InvalidURL) as unusable:
        raise ValueError(f"robots.txt redirected to an unusable URL ({unusable})") from unusable
    except (URLError, OSError, HTTPException) as failed:
        by_policy = [reason for reason in refused if not is_connection_failure(reason)]
        if by_policy:
            return f"robots.txt refused by the URL policy ({by_policy[0]}), so it disallows"
        cause = failed.reason if isinstance(failed, URLError) else failed
        if isinstance(cause, str):
            # ``UnknownHandler``'s words: the site redirected to a scheme not read here.
            return f"robots.txt redirected outside HTTP ({cause}), so it disallows"
        if isinstance(cause, ssl.SSLCertVerificationError):
            raise RobotsUnreachable(
                f"certificate not trusted on this machine; robots.txt unread ({cause})"
            ) from failed
        raise RobotsUnreachable(f"robots.txt could not be fetched ({failed})") from failed
    parser = RobotFileParser(robots_url)
    parser.parse(raw.decode("utf-8", errors="replace").splitlines())
    if not parser.can_fetch(USER_AGENT, url):
        return "robots.txt disallows this path"
    return None


async def wcc_visit(url: str, record: WccRecord, *, allow_private_network: bool = False) -> None:
    """What WCC offers on ``url``, compiled cold, as ``measure_coverage.visit`` does."""
    driver = _VisibleTextDriver(
        UrlPolicy(),
        DriverSettings(allow_private_network=allow_private_network, limits=ResourceLimits()),
    )
    store_directory = TemporaryDirectory(prefix="wcc-heldout-")
    store = Store(Path(store_directory.name) / "wcc.sqlite3")
    try:
        coordinator = Coordinator(driver, store, enricher=Enricher())
        session = await coordinator.open_session(OpenSessionRequest(url=url))
        elements = await driver.extract_elements(session.session_id)
        record.interactive_elements = len(elements)
        record.forms_seen = len({e.form_key for e in elements if e.form_key})

        inspection = await coordinator.inspect_page(session.session_id)
        snapshot = inspection.snapshot
        record.final_url = snapshot.url
        by_id = {element.element_id: element for element in snapshot.elements}
        seen: dict[str, str] = {}

        async def visible(element: NormalizedElement) -> str:
            if element.element_id not in seen:
                text = await driver.visible_text(session.session_id, element.element_id)
                if text is None:
                    record.visible_text_unread += 1
                    text = _name_of(element)
                seen[element.element_id] = text
            return seen[element.element_id]

        links = find_result_links(snapshot.elements, snapshot.collections)
        record.result_list = [await visible(link) for link in links]
        record.result_list_names = [_name_of(link) for link in links]
        for binding in inspection.capabilities:
            template = inspection.templates[binding.template_id]
            labels: list[str] = []
            names: list[str] = []
            for evidence in template.evidence:
                element = by_id.get(evidence.element_id or "")
                if element is not None:
                    shown, name = await visible(element), _name_of(element)
                    if shown and shown not in labels:
                        labels.append(shown)
                    if name and name not in names:
                        names.append(name)
            record.offered.append(
                {
                    "name": template.name,
                    "category": str(template.category),
                    "risk": str(template.risk.level),
                    "text_parameters": free_text_parameters(template.parameters),
                    "labels": labels,
                    "names": names,
                }
            )
        record.unbound = [f"{t.name}: {t.reason}" for t in inspection.unbound]
        if inspection.report is not None:
            for candidate in inspection.report.rejected:
                record.refused_codes.append(candidate.code)
                if candidate.risk is not None and candidate.risk.level is not RiskLevel.LOW:
                    record.refused_above_low += 1
    except DriverError as refused:
        record.error = f"{refused.code}: {refused.message}"
    except Exception as failed:
        record.error = f"{type(failed).__name__}: {failed}"
    finally:
        store.close()
        store_directory.cleanup()
        await _shut_down(driver)


def _name_of(element: NormalizedElement) -> str:
    return element.resolved_label or element.accessible_name or ""


def free_text_parameters(schema: dict[str, object]) -> int:
    """How many arguments take free text: a string with no fixed list of values."""
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return 0
    return sum(
        1
        for spec in properties.values()
        if isinstance(spec, dict) and spec.get("type") == "string" and "enum" not in spec
    )


# -- the run's files ---------------------------------------------------------


@dataclass
class RunFiles:
    out: Path

    @property
    def captures(self) -> Path:
        return self.out / "captures"

    @property
    def index(self) -> Path:
        return self.captures / "index.json"

    @property
    def wcc(self) -> Path:
        return self.out / "wcc" / "results.json"

    @property
    def labels(self) -> Path:
        return self.out / "labels.json"

    @property
    def meta(self) -> Path:
        return self.out / "run.json"


def load_index(files: RunFiles) -> dict[str, CaptureRecord]:
    if not files.index.exists():
        return {}
    return {
        r["key"]: CaptureRecord(**r) for r in json.loads(files.index.read_text(encoding="utf-8"))
    }


def load_wcc(files: RunFiles) -> dict[str, WccRecord]:
    if not files.wcc.exists():
        return {}
    return {r["key"]: WccRecord(**r) for r in json.loads(files.wcc.read_text(encoding="utf-8"))}


def excluded_by_labels(files: RunFiles) -> set[str]:
    """Keys the annotator excluded at Q0. Read for its keys only, never its answers."""
    if not files.labels.exists():
        return set()
    labels = json.loads(files.labels.read_text(encoding="utf-8")).get("labels", {})
    return {key for key, label in labels.items() if not label.get("eligible", True)}


def _write(path: Path, records: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([asdict(r) for r in records], indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _git(*args: str) -> str:
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return ""
    return done.stdout.strip()


def code_sha256() -> str:
    """One digest of the code a visit runs, including what is not committed yet.

    The commit alone cannot say this: an edit to ``src`` leaves ``HEAD`` where it was.
    """
    paths = [
        *(REPO_ROOT / "src").rglob("*.py"),
        *Path(__file__).parent.glob("*.py"),
        REPO_ROOT / "benchmarks" / "measure_coverage.py",
    ]
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(REPO_ROOT).as_posix().encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def run_metadata() -> dict[str, Any]:
    return {
        "started_at": _now(),
        "commit": _git("rev-parse", "HEAD"),
        "src_changed_since_commit": bool(_git("status", "--porcelain", "--", "src")),
        "code_sha256": code_sha256(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "playwright": version("playwright"),
        "plan_sha256": _sha256(Path(__file__).with_name("PLAN.md")),
        "plan_py_sha256": _sha256(Path(plan.__file__)),
        "sample_sha256": _sha256(SAMPLE_PATH),
    }


def _commit_mismatch(started: str, current: str, out: Path) -> str | None:
    """Why ``current`` is not the commit the run started at, or ``None``.

    ``PLAN.md`` has the annotator commit labels, and a stopped run may commit what it
    has so far. A commit that touches only this run's own directory changes no code,
    so it does not stop the run. Anything else, or a diff git cannot give, does.
    """
    if not started or not current:
        return f"the commit cannot be compared (started at {started!r}, now {current!r})"
    if started == current:
        return None
    changed = _git("diff", "--name-only", started, current).splitlines()
    try:
        own = out.resolve().relative_to(REPO_ROOT).as_posix() + "/"
    except ValueError:
        own = None
    if changed and own is not None and all(path.startswith(own) for path in changed):
        return None
    return f"the run started at commit {started}, and this is {current}"


def resume_mismatches(recorded: dict[str, Any], now: dict[str, Any], out: Path) -> list[str]:
    """What differs from when the run started. A missing fingerprint counts as different."""
    problems = [
        f"{key}: the run started with {recorded.get(key)}, and this is {now[key]}"
        for key in ("code_sha256", "plan_sha256", "plan_py_sha256", "sample_sha256")
        if recorded.get(key) != now[key]
    ]
    commit = _commit_mismatch(str(recorded.get("commit") or ""), now["commit"], out)
    return [commit, *problems] if commit else problems


def uncommitted(path: Path) -> str | None:
    """Why ``path`` is not committed as it stands, or ``None``. Unsure counts as no."""
    try:
        relative = path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return f"{path} is outside the repository, so it cannot be committed"
    if not _git("ls-files", "--", relative):
        return f"{path} is not committed"
    if _git("status", "--porcelain", "--", relative):
        return f"{path} has changes that are not committed"
    return None


Capture = Callable[[str, Path, Path, CaptureRecord], Awaitable[None]]
Visit = Callable[[str, WccRecord], Awaitable[None]]
Robots = Callable[[str], str | None]
Landing = Callable[[str, dict[str, str]], str | None]


async def _visit_one(
    candidate: Candidate,
    record: CaptureRecord,
    files: RunFiles,
    kept: dict[str, str],
    *,
    robots: Robots,
    take_capture: Capture,
    visit: Visit,
    landing: Landing,
    seconds: float,
) -> WccRecord | None:
    """Fill in ``record``, and return WCC's result if the visit has one worth keeping."""
    stem = record.key.replace("/", "-")
    shot, text = files.captures / f"{stem}.png", files.captures / f"{stem}.txt"
    phase = "robots.txt"
    outcome: WccRecord | None = None
    limit = asyncio.timeout(seconds)
    try:
        async with limit:
            refusal = await asyncio.to_thread(robots, candidate.url)
            if refusal is not None:
                record.status, record.reason = "robots", refusal
                return None
            phase = "the capture"
            record.captured_at = _now()
            await take_capture(candidate.url, shot, text, record)
            elsewhere = landing(record.final_url, kept)
            if elsewhere is not None:
                record.status, record.reason = "landed_elsewhere", elsewhere
                return None
            record.status = "captured"
            record.screenshot, record.text = shot.name, text.name
            record.screenshot_sha256 = _sha256(shot) if shot.exists() else ""
            record.text_sha256 = _sha256(text) if text.exists() else ""
            phase = "the WCC visit"
            outcome = WccRecord(record.key, candidate.url, observed_at=_now())
            await visit(candidate.url, outcome)
    except Exception as failed:
        cut_off = limit.expired()
        reason = (
            f"timed out after {seconds:g} s during {phase}"
            if cut_off
            else f"{type(failed).__name__}: {failed}"
        )
        if outcome is not None:
            outcome.error = reason
        else:
            connection = isinstance(failed, RobotsUnreachable) or is_connection_failure(reason)
            record.status = "unreachable" if connection and not cut_off else "failed"
            record.reason = reason
            return None

    assert outcome is not None
    if outcome.error is not None and is_connection_failure(outcome.error):
        record.status, record.reason = "unreachable", f"the WCC visit: {outcome.error}"
        return None
    record.wcc_complete = outcome.complete
    elsewhere = landing(outcome.final_url, kept)
    if elsewhere is not None:
        record.status, record.reason = "landed_elsewhere", f"the WCC visit: {elsewhere}"
    return outcome


async def visit_candidates(
    files: RunFiles,
    candidates: list[Candidate],
    *,
    robots: Robots = guarded_robots_refusal,
    take_capture: Capture = capture,
    visit: Visit = wcc_visit,
    pause_seconds: float = PAUSE_SECONDS,
    per_stratum: int = plan.PAGES_PER_STRATUM,
    log: Callable[[str], None] = print,
    candidate_timeout: float = CANDIDATE_TIMEOUT_SECONDS,
    landing: Landing = landing_exclusion,
) -> dict[str, int]:
    """Visit in order until each stratum has ``per_stratum`` complete, unexcluded pages.

    A candidate is skipped if it has a final record. One the connection failed on
    does not, so resuming tries it again. ``landing`` is a parameter because the
    loopback tests serve every candidate from one host, which E4 would rule out.
    Returns how many such pages each stratum has afterwards.
    """
    files.captures.mkdir(parents=True, exist_ok=True)
    index = load_index(files)
    results = load_wcc(files)
    excluded = excluded_by_labels(files)

    def is_page(key: str, record: CaptureRecord) -> bool:
        return (
            record.status == "captured"
            and key in results
            and results[key].complete
            and key not in excluded
        )

    def counted(stratum: str) -> int:
        return sum(
            1 for key, record in index.items() if record.stratum == stratum and is_page(key, record)
        )

    def kept_sites() -> dict[str, str]:
        """E4's "already kept": each page so far, by where it started and where it ended."""
        sites: dict[str, str] = {}
        for key, record in index.items():
            if is_page(key, record):
                for url in (record.url, record.final_url, results[key].final_url):
                    host = urlsplit(url).hostname
                    if host:
                        sites.setdefault(site_key(host), key)
        return sites

    for candidate in candidates:
        key = key_of(candidate)
        final = key in index and index[key].status not in RETRIED_ON_RESUME
        if final or counted(candidate.stratum) >= per_stratum:
            continue
        log(f"  {key} {candidate.url}")
        record = CaptureRecord(
            key, candidate.stratum, candidate.position, candidate.origin, candidate.url
        )
        outcome = await _visit_one(
            candidate,
            record,
            files,
            kept_sites(),
            robots=robots,
            take_capture=take_capture,
            visit=visit,
            landing=landing,
            seconds=candidate_timeout,
        )
        if outcome is not None:
            results[key] = outcome
            _write(files.wcc, list(results.values()))
        index[key] = record
        _write(files.index, list(index.values()))
        log(f"    {record.status} {record.reason[:80]}")
        await asyncio.sleep(pause_seconds)

    return {stratum.name: counted(stratum.name) for stratum in plan.STRATA}


def run_all(out: Path, *, only_stratum: str = "") -> int:
    files = RunFiles(out)
    out.mkdir(parents=True, exist_ok=True)
    now = run_metadata()
    if files.meta.exists():
        recorded = json.loads(files.meta.read_text(encoding="utf-8"))
        problems = resume_mismatches(recorded, now, out)
    elif files.index.exists() or files.wcc.exists():
        problems = [f"{files.meta} is missing, but this directory already has results"]
    else:
        files.meta.write_text(json.dumps(now, indent=2) + "\n", encoding="utf-8")
        problems = []
    if problems:
        print(f"not resumed: {out} was started under different conditions.")
        for problem in problems:
            print(f"  {problem}")
        print("Restore them, or start a new run in a new directory.")
        return 2
    candidates = load_sample()
    if only_stratum:
        candidates = [c for c in candidates if c.stratum == only_stratum]
    counts = asyncio.run(visit_candidates(files, candidates))
    for stratum, count in counts.items():
        short = "" if count >= plan.PAGES_PER_STRATUM else "  (short)"
        print(f"{stratum}: {count} of {plan.PAGES_PER_STRATUM} complete pages{short}")
    waiting = sum(1 for r in load_index(files).values() if r.status in RETRIED_ON_RESUME)
    if waiting:
        print(f"{waiting} candidates could not be reached. Run this again to try them again.")
    print(f"\nNext: label without opening {files.wcc}.")
    return 0
