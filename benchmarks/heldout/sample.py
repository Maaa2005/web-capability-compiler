"""Draw the held-out candidates from the CrUX frame, exactly as ``PLAN.md`` says.

Pure apart from reading the frame file, so the draw can be repeated by anyone
holding the same file and checked against the committed ``sample.json``.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import random
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from benchmarks.heldout import plan

SAMPLE_PATH = Path(__file__).resolve().parent / "sample.json"


@dataclass(frozen=True)
class Candidate:
    stratum: str
    position: int
    """1-based order within the stratum. Candidates are used strictly in this order."""

    origin: str
    rank_bucket: int

    @property
    def url(self) -> str:
        return f"{self.origin}/"

    @property
    def host(self) -> str:
        return urlsplit(self.origin).hostname or ""


@dataclass
class Draw:
    candidates: list[Candidate]
    excluded: Counter[str] = field(default_factory=Counter)
    """How many origins each rule E1 to E4 removed while walking the shuffles."""


class FrameMismatch(Exception):
    """The frame file is not the one the plan names."""


def read_frame(path: Path, expected_sha256: str = plan.FRAME_SHA256) -> list[tuple[str, int]]:
    """Read ``origin,rank`` rows, refusing a file whose hash is not the plan's."""
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise FrameMismatch(f"{path} has SHA-256 {digest}; the plan names {expected_sha256}")
    text = gzip.decompress(raw).decode("utf-8")
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        rows.append((row["origin"].strip(), int(row["rank"])))
    return rows


def on_development_domain(host: str) -> bool:
    return any(host == d or host.endswith(f".{d}") for d in plan.DEVELOPMENT_DOMAINS)


def exclusion(origin: str) -> str | None:
    """The rule among E1 to E3 that excludes ``origin``, or ``None``."""
    parts = urlsplit(origin)
    if parts.scheme != "https":
        return "E1"
    host = (parts.hostname or "").lower()
    if on_development_domain(host):
        return "E2"
    if any(word in host for word in plan.ADULT_HOST_WORDS):
        return "E3"
    return None


def site_key(host: str) -> str:
    """E4's notion of "the same site": the host without a leading ``www.`` or ``m.``."""
    host = host.lower()
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            return host[len(prefix) :]
    return host


def landing_exclusion(final_url: str, kept: Mapping[str, str]) -> str | None:
    """E2 or E4 for the page a candidate ended on, which the draw could not see.

    The draw judged the origin. A redirect can still put a development site in the
    run, or a second page of a site already kept. ``kept`` maps each site so far to
    its page's key.
    """
    host = (urlsplit(final_url).hostname or "").lower()
    if not host:
        return None
    if on_development_domain(host):
        return f"E2: it landed on {host}, a development domain"
    earlier = kept.get(site_key(host))
    return None if earlier is None else f"E4: it landed on {host}, the site of {earlier}"


def draw(
    rows: list[tuple[str, int]],
    seed: int = plan.SEED,
    strata: tuple[plan.Stratum, ...] = plan.STRATA,
    per_stratum: int = plan.CANDIDATES_PER_STRATUM,
) -> Draw:
    rng = random.Random(seed)
    result = Draw(candidates=[])
    kept_sites: set[str] = set()
    for stratum in strata:
        pool = sorted(origin for origin, rank in rows if rank in stratum.ranks)
        ranks = {origin: rank for origin, rank in rows if rank in stratum.ranks}
        rng.shuffle(pool)
        position = 0
        for origin in pool:
            if position == per_stratum:
                break
            rule = exclusion(origin)
            if rule is None:
                key = site_key(urlsplit(origin).hostname or "")
                if key in kept_sites:
                    rule = "E4"
                else:
                    kept_sites.add(key)
            if rule is not None:
                result.excluded[rule] += 1
                continue
            position += 1
            result.candidates.append(Candidate(stratum.name, position, origin, ranks[origin]))
    return result


def sample_document(result: Draw) -> dict[str, object]:
    return {
        "plan": "benchmarks/heldout/PLAN.md",
        "frame": {
            "repository": plan.FRAME_REPOSITORY,
            "commit": plan.FRAME_COMMIT,
            "path": plan.FRAME_PATH,
            "sha256": plan.FRAME_SHA256,
        },
        "seed": plan.SEED,
        "strata": [asdict(s) for s in plan.STRATA],
        "candidates_per_stratum": plan.CANDIDATES_PER_STRATUM,
        "excluded_while_drawing": dict(sorted(result.excluded.items())),
        "candidates": [asdict(c) for c in result.candidates],
    }


def load_sample(path: Path = SAMPLE_PATH) -> list[Candidate]:
    import json

    document = json.loads(path.read_text(encoding="utf-8"))
    return [Candidate(**entry) for entry in document["candidates"]]
