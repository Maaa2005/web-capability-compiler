"""A filtering proxy between the browser and the network (specification 10.2).

``context.route`` sees HTTP requests and nothing else: a WebSocket, a Worker's
traffic, and the connection Chromium actually opens all go past it, and the
address Chromium connects to is whatever DNS answers *then*. Playwright's own
WebSocket hook can be bypassed from page script. The place every one of those has
to pass is the socket, so the browser is pointed at this proxy and the proxy
decides.

For each destination it does what Stripe's Smokescreen and Advocate do, and what
the OWASP SSRF cheat sheet asks for: resolve the name itself, judge **every**
address it got, then connect to one of those addresses by literal. The name is
never resolved a second time, so there is no window between the check and the
connection for a rebinding answer to use. Judging goes through the same
:class:`NavigationGuard` as every other URL, with the address written as a URL, so
there is no second policy here to drift out of step with the first.

Scope, stated plainly:

- HTTPS, WebSocket (``ws``/``wss`` use ``CONNECT``) and HTTP/1.1 requests in
  absolute form are handled; nothing is decrypted.
- Chromium proxies ``localhost``, ``127.0.0.0/8`` and ``169.254.0.0/16`` around a
  proxy unless told otherwise. Playwright adds ``<-loopback>`` itself when a proxy
  is configured, which is what makes the loopback fixture and the metadata address
  reach this proxy at all.
- UDP does not pass through an HTTP proxy. The driver also disables Chromium's
  non-proxied WebRTC UDP; WebTransport and HTTP/3 have no documented behaviour
  with an HTTP proxy and are not covered by this module.

Not a general-purpose proxy: it keeps no connection alive past one request, speaks
no authentication, and exists for one browser context.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from wcc.driver.base import NavigationBlockedError, NavigationGuard

logger = logging.getLogger(__name__)

#: Reads the first line and headers of a request. A client sending more than this
#: before a blank line is not a browser.
_MAX_HEAD_BYTES = 64 * 1024
_HEAD_TIMEOUT_S = 30.0
_CONNECT_TIMEOUT_S = 15.0
#: Silence on an established tunnel. Long enough for a WebSocket that pings once a
#: minute, short enough that a dead peer does not hold a descriptor for ever.
_IDLE_TIMEOUT_S = 300.0
#: Resolving and connecting together. ``getaddrinfo`` has no timeout of its own and
#: runs on a shared thread pool, so a name that never answers is cut off here and
#: only a few lookups may be in flight at once.
_CONNECT_DEADLINE_S = 30.0
_MAX_RESOLVING = 32
_MAX_CONNECTIONS = 256
_CHUNK = 64 * 1024
#: How long one direction may keep going after the other has ended.
_GRACE_S = 5.0

#: Headers that belong to the hop between browser and proxy.
_HOP_HEADERS = frozenset({"proxy-connection", "proxy-authorization", "proxy-authenticate"})

Resolver = Callable[[str, int], Awaitable[list[str]]]
Refusal = Callable[[str, str], None]


async def resolve_host(host: str, port: int) -> list[str]:
    """Every address ``host`` currently has, in the order the system prefers."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise NavigationBlockedError(f"{host!r} does not resolve: {exc}") from exc
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


class _Refused(Exception):
    """The proxy declines this request; ``status`` is what the browser is told."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class EgressProxy:
    """A loopback-only proxy that connects only to addresses the guard admits.

    ``allow_private_network`` is a callable rather than a flag because the session
    owning this proxy can have its allowance changed after the proxy starts, and a
    copy taken at construction would keep answering with the old one.
    """

    def __init__(
        self,
        guard: NavigationGuard,
        *,
        allow_private_network: Callable[[], bool],
        on_refused: Refusal,
        resolver: Resolver = resolve_host,
        idle_timeout_s: float = _IDLE_TIMEOUT_S,
        connect_deadline_s: float = _CONNECT_DEADLINE_S,
    ) -> None:
        self._guard = guard
        self._allow_private_network = allow_private_network
        self._on_refused = on_refused
        self._resolver = resolver
        self._idle_timeout_s = idle_timeout_s
        self._connect_deadline_s = connect_deadline_s
        self._resolving = asyncio.Semaphore(_MAX_RESOLVING)
        self._closed = False
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self.server_url: str = ""

    async def start(self) -> str:
        """Listen on an ephemeral loopback port and return the proxy URL."""
        self._server = await asyncio.start_server(
            self._accept, host="127.0.0.1", port=0, limit=_MAX_HEAD_BYTES
        )
        port = self._server.sockets[0].getsockname()[1]
        self.server_url = f"http://127.0.0.1:{port}"
        return self.server_url

    async def close(self) -> None:
        """Stop listening and cut every open tunnel."""
        self._closed = True
        server, self._server = self._server, None
        if server is not None:
            server.close()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if server is not None:
            await server.wait_closed()

    # -- one client connection ---------------------------------------------

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            if self._closed or len(self._tasks) > _MAX_CONNECTIONS:
                await self._reply(writer, 503, "too many connections")
                return
            await self._serve(reader, writer)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("egress proxy: a connection failed")
        finally:
            if task is not None:
                self._tasks.discard(task)
            await _close(writer)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        target = "(unknown)"
        method = ""
        path = ""
        try:
            head = await self._read_head(reader)
            method, target, lines = _parse_head(head)
            if method == "CONNECT":
                host, port = _split_authority(target)
                shown = f"{host}:{port}"
            else:
                host, port, path = _split_absolute(target)
                shown = f"http://{host}:{port}{path}"
            target = shown
            try:
                upstream = await asyncio.wait_for(
                    self._admit_and_connect(host, port, shown), self._connect_deadline_s
                )
            except TimeoutError as exc:
                raise _Refused(
                    403, f"{shown} did not finish resolving and connecting in time"
                ) from exc
        except _Refused as refused:
            if refused.status == 403:
                self._on_refused(target, refused.reason)
                if method != "CONNECT":
                    # An error page would be shown as if the destination had sent
                    # it, and a navigation to it would "succeed". Hanging up makes
                    # the request fail, which is what a refusal is.
                    return
            if method != "CONNECT" and refused.status in (502, 503):
                return
            await self._reply(writer, refused.status, refused.reason)
            return

        upstream_reader, upstream_writer = upstream
        try:
            if method == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:
                upstream_writer.write(_forward_head(method, path, lines))
                await upstream_writer.drain()
            activity = [asyncio.get_running_loop().time()]
            await self._tunnel(
                asyncio.ensure_future(self._pump(reader, upstream_writer, activity)),
                asyncio.ensure_future(self._pump(upstream_reader, writer, activity)),
            )
        finally:
            await _close(upstream_writer)

    @staticmethod
    async def _tunnel(*pumps: asyncio.Future[None]) -> None:
        """Run both directions until one ends, then give the other a moment and stop it.

        A connection whose one side has gone away must not hold a descriptor for the
        other side's idle timeout, and a plain request that has been answered must
        not stay open for the next one to ride on (it was only checked against the
        first request's host).
        """
        try:
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            await asyncio.wait(pumps, timeout=_GRACE_S)
        finally:
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)

    async def _admit_and_connect(
        self, host: str, port: int, shown: str
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        addresses = await self._admit(host, port)
        return await self._connect(addresses, port, shown)

    async def _read_head(self, reader: asyncio.StreamReader) -> bytes:
        try:
            return await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), _HEAD_TIMEOUT_S)
        except asyncio.LimitOverrunError as exc:
            raise _Refused(431, "request head too large") from exc
        except (asyncio.IncompleteReadError, TimeoutError, ConnectionError) as exc:
            raise _Refused(400, "incomplete request") from exc

    # -- deciding ----------------------------------------------------------

    async def _admit(self, host: str, port: int) -> list[str]:
        """Resolve ``host`` and return its addresses, all of which the guard admits.

        The string the client sent is never judged on its own. ``0x7f.1`` and
        ``2130706433`` reach loopback through ``getaddrinfo``, and a check of the
        text would not know it; the check of what ``getaddrinfo`` returned does.
        """
        literal = _literal_address(host)
        if literal is not None:
            addresses = [literal]
        else:
            try:
                async with self._resolving:
                    addresses = await self._resolver(host, port)
            except NavigationBlockedError as exc:
                raise _Refused(403, exc.message) from exc
        if not addresses:
            raise _Refused(403, f"{host!r} resolved to no addresses")
        for address in addresses:
            if "%" in address:
                raise _Refused(403, f"{host!r} resolved to the scoped address {address}")
            bracketed = f"[{address}]" if ":" in address else address
            try:
                self._guard.check(
                    f"http://{bracketed}/",
                    allow_private_network=self._allow_private_network(),
                )
            except NavigationBlockedError as blocked:
                raise _Refused(
                    403,
                    f"{host!r} resolves to {address}, which the policy refuses: {blocked.message}",
                ) from blocked
        return addresses

    async def _connect(
        self, addresses: list[str], port: int, shown: str
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Connect to a vetted address, by literal, so nothing is looked up again."""
        last: Exception | None = None
        for address in addresses:
            try:
                return await asyncio.wait_for(
                    asyncio.open_connection(address, port), _CONNECT_TIMEOUT_S
                )
            except (OSError, TimeoutError) as exc:
                last = exc
        raise _Refused(502, f"could not connect to {shown}: {last}")

    # -- moving bytes ------------------------------------------------------

    async def _pump(
        self, source: asyncio.StreamReader, sink: asyncio.StreamWriter, activity: list[float]
    ) -> None:
        """Copy until EOF. Idle means the whole tunnel was silent, not this direction.

        A server streaming to a browser that sends nothing is busy, and half-closing
        the upstream because the browser was quiet would cut the stream.
        """
        loop = asyncio.get_running_loop()
        try:
            while True:
                try:
                    data = await asyncio.wait_for(source.read(_CHUNK), self._idle_timeout_s)
                except TimeoutError:
                    if loop.time() - activity[0] < self._idle_timeout_s:
                        continue
                    break
                if not data:
                    break
                activity[0] = loop.time()
                sink.write(data)
                await sink.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            # Half-close, so the other direction can finish what it is sending.
            with contextlib.suppress(Exception):
                if sink.can_write_eof():
                    sink.write_eof()

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, status: int, reason: str) -> None:
        body = reason.encode("utf-8", "replace")
        head = (
            f"HTTP/1.1 {status} {_PHRASES.get(status, 'Error')}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        with contextlib.suppress(Exception):
            writer.write(head + body)
            await writer.drain()


_PHRASES = {
    400: "Bad Request",
    403: "Forbidden",
    431: "Request Header Fields Too Large",
    501: "Not Implemented",
    502: "Bad Gateway",
    503: "Service Unavailable",
}


async def _close(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(Exception):
        writer.close()
        await writer.wait_closed()


# -- parsing ---------------------------------------------------------------


def _parse_head(head: bytes) -> tuple[str, str, list[tuple[str, str]]]:
    try:
        text = head.decode("latin-1")
    except UnicodeDecodeError as exc:  # pragma: no cover - latin-1 decodes every byte
        raise _Refused(400, "unreadable request") from exc
    request_line, *header_lines = text.split("\r\n")
    if any("\n" in line or "\r" in line for line in [request_line, *header_lines]):
        raise _Refused(400, "bare line break in the request head")
    parts = request_line.split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise _Refused(400, "malformed request line")
    method, target, _ = parts
    headers: list[tuple[str, str]] = []
    for line in header_lines:
        if not line:
            continue
        if line[0] in " \t" or ":" not in line:
            raise _Refused(400, "malformed header")
        name, _, value = line.partition(":")
        headers.append((name.strip(), value.strip()))
    return method, target, headers


def _split_authority(authority: str) -> tuple[str, int]:
    """``host:port`` or ``[v6]:port`` from a CONNECT target; the port is required."""
    if authority.startswith("["):
        host, sep, rest = authority[1:].partition("]")
        if not sep or not rest.startswith(":"):
            raise _Refused(400, "malformed CONNECT target")
        port_text = rest[1:]
    else:
        host, sep, port_text = authority.rpartition(":")
        if not sep:
            raise _Refused(400, "malformed CONNECT target")
    if not host or not port_text.isdigit() or not 0 < int(port_text) < 65536:
        raise _Refused(400, "malformed CONNECT target")
    return host, int(port_text)


def _split_absolute(target: str) -> tuple[str, int, str]:
    """Host, port and origin-form path of an absolute-form ``http`` request target."""
    try:
        parts = urlsplit(target)
        port = parts.port
    except ValueError as exc:
        raise _Refused(400, "malformed request target") from exc
    if parts.scheme.lower() != "http" or not parts.hostname:
        raise _Refused(400, "only absolute http:// targets and CONNECT are proxied")
    if parts.username is not None or parts.password is not None:
        raise _Refused(403, "a URL carrying credentials is refused")
    if port == 0:
        raise _Refused(400, "port 0")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return parts.hostname, port or 80, path


def _literal_address(host: str) -> str | None:
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


def _forward_head(method: str, path: str, headers: list[tuple[str, str]]) -> bytes:
    """Rebuild the request for the origin: origin-form target, hop headers dropped.

    One request per connection, unless the client is upgrading it (a plain-text
    WebSocket). That keeps the proxy from having to frame bodies: after the head
    the rest of the connection is copied through, and ``Connection: close`` makes
    the origin end it after the response.
    """
    upgrading = any(name.lower() == "upgrade" for name, _ in headers)
    lines = [f"{method} {path} HTTP/1.1"]
    for name, value in headers:
        lowered = name.lower()
        if lowered in _HOP_HEADERS:
            continue
        if lowered == "connection" and not upgrading:
            continue
        lines.append(f"{name}: {value}")
    if not upgrading:
        lines.append("Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
