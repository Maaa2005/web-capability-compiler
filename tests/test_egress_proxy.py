"""The filtering proxy of specification 10.2, driven with raw sockets.

No browser is involved: a client speaks to the proxy the way Chromium does
(``CONNECT host:port`` for HTTPS and WebSocket, absolute-form requests for plain
HTTP) and the destination is a small loopback server. The resolver is faked, so the
rules are tested and not the machine's DNS.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from wcc.driver.base import NavigationBlockedError
from wcc.driver.egress_proxy import EgressProxy, resolve_host
from wcc.policy.url_policy import UrlPolicy


class _Destination:
    """A loopback server that records plain request heads and echoes tunnels."""

    def __init__(self) -> None:
        self.heads: list[bytes] = []
        self.port = 0
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> _Destination:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            first = await reader.read(1)
            if first == b"G":  # an HTTP request in plain form
                head = first + await reader.readuntil(b"\r\n\r\n")
                self.heads.append(head)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            else:  # a tunnel: echo until the client stops sending
                writer.write(first)
                while data := await reader.read(1024):
                    writer.write(data)
                    await writer.drain()
        finally:
            writer.close()


class _Client:
    def __init__(self, port: int) -> None:
        self.port = port

    async def send(self, raw: bytes) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(raw)
        await writer.drain()
        return reader, writer


Refusals = list[tuple[str, str]]


def _make(
    answers: dict[str, list[str]] | None = None,
    *,
    allow_private: bool = True,
    resolved: list[str] | None = None,
) -> tuple[EgressProxy, Refusals]:
    refusals: Refusals = []

    async def resolver(host: str, port: int) -> list[str]:
        del port
        if resolved is not None:
            resolved.append(host)
        if answers is None or host not in answers:
            raise NavigationBlockedError(f"{host!r} does not resolve")
        return answers[host]

    proxy = EgressProxy(
        UrlPolicy(),
        allow_private_network=lambda: allow_private,
        on_refused=lambda url, reason: refusals.append((url, reason)),
        resolver=resolver,
        idle_timeout_s=5,
    )
    return proxy, refusals


@pytest.fixture
async def started() -> AsyncIterator[list[EgressProxy]]:
    made: list[EgressProxy] = []
    yield made
    for proxy in made:
        await proxy.close()


async def _start(started: list[EgressProxy], proxy: EgressProxy) -> _Client:
    started.append(proxy)
    url = await proxy.start()
    return _Client(int(url.rsplit(":", 1)[1]))


async def _status(reader: asyncio.StreamReader) -> bytes:
    return (await asyncio.wait_for(reader.readline(), 5)).strip()


async def test_a_tunnel_to_an_admitted_address_carries_bytes_both_ways(
    started: list[EgressProxy],
) -> None:
    async with _Destination() as destination:
        proxy, refusals = _make({"fixture.test": ["127.0.0.1"]})
        client = await _start(started, proxy)

        reader, writer = await client.send(
            f"CONNECT fixture.test:{destination.port} HTTP/1.1\r\nHost: x\r\n\r\n".encode()
        )
        assert await _status(reader) == b"HTTP/1.1 200 Connection Established"
        await reader.readline()

        writer.write(b"\x81hello")
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(6), 5) == b"\x81hello"
        writer.close()
        assert refusals == []


@pytest.mark.parametrize(
    "answer",
    [
        ["127.0.0.1"],
        ["::1"],
        ["10.0.0.5"],
        ["169.254.169.254"],
        ["::ffff:169.254.169.254"],
        ["64:ff9b::7f00:1"],
        ["64:ff9b::a9fe:a9fe"],
        ["2002:7f00:1::"],
        ["::127.0.0.1"],
        ["192.88.99.1"],
        ["100.64.0.1"],
        ["93.184.216.34", "127.0.0.1"],
    ],
)
async def test_a_destination_with_any_refused_address_is_refused(
    started: list[EgressProxy], answer: list[str]
) -> None:
    proxy, refusals = _make({"evil.test": answer}, allow_private=False)
    client = await _start(started, proxy)

    reader, writer = await client.send(b"CONNECT evil.test:443 HTTP/1.1\r\n\r\n")

    assert await _status(reader) == b"HTTP/1.1 403 Forbidden"
    writer.close()
    assert len(refusals) == 1
    assert refusals[0][0] == "evil.test:443"


async def test_metadata_is_refused_even_when_the_private_network_is_allowed(
    started: list[EgressProxy],
) -> None:
    proxy, _ = _make({"m.test": ["169.254.169.254"]}, allow_private=True)
    client = await _start(started, proxy)

    reader, writer = await client.send(b"CONNECT m.test:80 HTTP/1.1\r\n\r\n")

    assert await _status(reader) == b"HTTP/1.1 403 Forbidden"
    writer.close()


async def test_a_name_is_resolved_once_and_the_answer_is_what_gets_connected_to(
    started: list[EgressProxy],
) -> None:
    """The rebinding window: a second lookup is the only way for the answer to change."""
    resolved: list[str] = []
    async with _Destination() as destination:
        proxy, _ = _make({"once.test": ["127.0.0.1"]}, resolved=resolved)
        client = await _start(started, proxy)
        reader, writer = await client.send(
            f"CONNECT once.test:{destination.port} HTTP/1.1\r\n\r\n".encode()
        )
        assert await _status(reader) == b"HTTP/1.1 200 Connection Established"
        writer.close()

    assert resolved == ["once.test"]


@pytest.mark.parametrize("host", ["2130706433", "0x7f.1", "0177.0.0.1", "127.1", "[::ffff:7f00:1]"])
async def test_ways_of_writing_loopback_are_refused_by_what_they_resolve_to(
    started: list[EgressProxy], host: str
) -> None:
    """The text is never judged; the address it becomes is."""
    refusals: Refusals = []
    proxy = EgressProxy(
        UrlPolicy(),
        allow_private_network=lambda: False,
        on_refused=lambda url, reason: refusals.append((url, reason)),
        resolver=resolve_host,
    )
    client = await _start(started, proxy)

    reader, writer = await client.send(f"CONNECT {host}:80 HTTP/1.1\r\n\r\n".encode())

    assert await _status(reader) == b"HTTP/1.1 403 Forbidden"
    writer.close()
    assert len(refusals) == 1


async def test_the_private_network_allowance_is_read_on_every_connection(
    started: list[EgressProxy],
) -> None:
    allowed = [True]

    async def resolver(host: str, port: int) -> list[str]:
        del host, port
        return ["127.0.0.1"]

    async with _Destination() as destination:
        proxy = EgressProxy(
            UrlPolicy(),
            allow_private_network=lambda: allowed[0],
            on_refused=lambda url, reason: None,
            resolver=resolver,
        )
        client = await _start(started, proxy)
        request = f"CONNECT a.test:{destination.port} HTTP/1.1\r\n\r\n".encode()

        reader, writer = await client.send(request)
        assert await _status(reader) == b"HTTP/1.1 200 Connection Established"
        writer.close()

        allowed[0] = False
        reader, writer = await client.send(request)
        assert await _status(reader) == b"HTTP/1.1 403 Forbidden"
        writer.close()


async def test_a_plain_request_reaches_the_origin_in_origin_form_without_proxy_headers(
    started: list[EgressProxy],
) -> None:
    async with _Destination() as destination:
        proxy, _ = _make({"site.test": ["127.0.0.1"]})
        client = await _start(started, proxy)

        reader, writer = await client.send(
            (
                f"GET http://site.test:{destination.port}/a/b?q=1 HTTP/1.1\r\n"
                f"Host: site.test:{destination.port}\r\n"
                "Proxy-Connection: keep-alive\r\n"
                "Proxy-Authorization: Basic eDp5\r\n"
                "Connection: keep-alive\r\n"
                "Accept: */*\r\n\r\n"
            ).encode()
        )
        answer = await asyncio.wait_for(reader.read(), 5)
        writer.close()

    assert answer.endswith(b"ok")
    head = destination.heads[0].decode()
    assert head.startswith("GET /a/b?q=1 HTTP/1.1\r\n")
    assert "proxy-" not in head.lower()
    assert head.lower().count("connection:") == 1
    assert "Connection: close" in head
    assert "Accept: */*" in head


async def test_a_refused_plain_request_gets_no_page_that_could_pass_for_the_destination(
    started: list[EgressProxy],
) -> None:
    proxy, refusals = _make({"evil.test": ["127.0.0.1"]}, allow_private=False)
    client = await _start(started, proxy)

    reader, writer = await client.send(b"GET http://evil.test/ HTTP/1.1\r\nHost: evil.test\r\n\r\n")

    assert await asyncio.wait_for(reader.read(), 5) == b""
    writer.close()
    assert [url for url, _ in refusals] == ["http://evil.test:80/"]


_BAD = b"HTTP/1.1 400 Bad Request"


@pytest.mark.parametrize(
    ("raw", "status"),
    [
        pytest.param(b"CONNECT nonsense HTTP/1.1\r\n\r\n", _BAD, id="no-port"),
        pytest.param(b"CONNECT host:99999 HTTP/1.1\r\n\r\n", _BAD, id="port-out-of-range"),
        pytest.param(b"CONNECT [::1 HTTP/1.1\r\n\r\n", _BAD, id="open-bracket"),
        pytest.param(b"GET /relative HTTP/1.1\r\n\r\n", _BAD, id="origin-form"),
        pytest.param(b"GET ftp://x/ HTTP/1.1\r\n\r\n", _BAD, id="other-scheme"),
        pytest.param(b"NONSENSE\r\n\r\n", _BAD, id="no-version"),
        pytest.param(b"GET http://x/ HTTP/1.1\r\n bad: fold\r\n\r\n", _BAD, id="folded-header"),
        pytest.param(
            b"GET http://x/ HTTP/1.1\r\nA: " + b"b" * 70_000 + b"\r\n\r\n",
            b"HTTP/1.1 431",
            id="head-too-large",
        ),
    ],
)
async def test_malformed_requests_are_answered_not_forwarded(
    started: list[EgressProxy], raw: bytes, status: bytes
) -> None:
    proxy, refusals = _make({})
    client = await _start(started, proxy)

    reader, writer = await client.send(raw)

    assert (await _status(reader)).startswith(status)
    writer.close()
    assert refusals == []


async def test_a_name_that_does_not_resolve_is_refused_with_its_reason(
    started: list[EgressProxy],
) -> None:
    proxy, refusals = _make({})
    client = await _start(started, proxy)

    reader, writer = await client.send(b"CONNECT nowhere.test:443 HTTP/1.1\r\n\r\n")

    assert await _status(reader) == b"HTTP/1.1 403 Forbidden"
    writer.close()
    assert "does not resolve" in refusals[0][1]


async def test_closing_the_proxy_cuts_open_tunnels(started: list[EgressProxy]) -> None:
    async with _Destination() as destination:
        proxy, _ = _make({"site.test": ["127.0.0.1"]})
        client = await _start(started, proxy)
        reader, writer = await client.send(
            f"CONNECT site.test:{destination.port} HTTP/1.1\r\n\r\n".encode()
        )
        assert await _status(reader) == b"HTTP/1.1 200 Connection Established"
        await reader.readline()

        await proxy.close()

        assert await asyncio.wait_for(reader.read(), 5) == b""
        writer.close()


async def _streamer(chunks: int, gap_s: float) -> tuple[asyncio.Server, int, list[str]]:
    """Sends a byte every ``gap_s`` and ignores input; stops if the client half-closes."""
    events: list[str] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async def watch() -> None:
            if await reader.read(1024) == b"":
                events.append("client-eof")

        watcher = asyncio.ensure_future(watch())
        try:
            for _ in range(chunks):
                if events:
                    break
                writer.write(b"x")
                await writer.drain()
                await asyncio.sleep(gap_s)
        finally:
            watcher.cancel()
            writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], events


async def test_a_tunnel_that_is_busy_in_one_direction_is_not_idle(
    started: list[EgressProxy],
) -> None:
    """Silence from the browser while the server streams is not an idle connection."""
    server, port, events = await _streamer(chunks=8, gap_s=0.3)
    try:
        proxy, _ = _make({"stream.test": ["127.0.0.1"]})
        proxy._idle_timeout_s = 1.0  # noqa: SLF001
        client = await _start(started, proxy)
        reader, writer = await client.send(f"CONNECT stream.test:{port} HTTP/1.1\r\n\r\n".encode())
        assert await _status(reader) == b"HTTP/1.1 200 Connection Established"
        await reader.readline()

        received = await asyncio.wait_for(reader.read(), 10)
        writer.close()
    finally:
        server.close()

    assert len(received) == 8
    assert events == []


async def test_a_name_that_never_resolves_is_given_up_on(started: list[EgressProxy]) -> None:
    async def hang(host: str, port: int) -> list[str]:
        del host, port
        await asyncio.sleep(3600)
        return []

    refusals: Refusals = []
    proxy = EgressProxy(
        UrlPolicy(),
        allow_private_network=lambda: False,
        on_refused=lambda url, reason: refusals.append((url, reason)),
        resolver=hang,
        connect_deadline_s=0.5,
    )
    client = await _start(started, proxy)

    reader, writer = await client.send(b"CONNECT slow.test:443 HTTP/1.1\r\n\r\n")

    assert await _status(reader) == b"HTTP/1.1 403 Forbidden"
    writer.close()
    assert "did not finish" in refusals[0][1]


async def test_a_closed_proxy_no_longer_accepts(started: list[EgressProxy]) -> None:
    proxy, _ = _make({})
    client = await _start(started, proxy)

    await proxy.close()

    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", client.port)


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"GET http://x.test:0/ HTTP/1.1\r\n\r\n", id="port-zero"),
        pytest.param(b"GET http://x.test/ HTTP/1.1\r\nA: b\nC: d\r\n\r\n", id="bare-newline"),
    ],
)
async def test_ambiguous_plain_requests_are_refused(started: list[EgressProxy], raw: bytes) -> None:
    proxy, _ = _make({"x.test": ["127.0.0.1"]})
    client = await _start(started, proxy)

    reader, writer = await client.send(raw)

    assert (await _status(reader)).startswith(b"HTTP/1.1 400")
    writer.close()
