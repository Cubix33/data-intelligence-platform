"""SSRF-safe outbound HTTP requests for untrusted crawl targets."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from contextlib import contextmanager
from typing import Iterator, Mapping
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx


class UnsafeDestination(ValueError):
    """Raised when a URL cannot be proven to resolve only to public addresses."""


_PINNED_DESTINATIONS: ContextVar[dict[tuple[str, int], str]] = ContextVar(
    "scout_pinned_destinations", default={}
)


def _host_key(host: str) -> str:
    host = host.strip("[]").rstrip(".").lower()
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        try:
            return host.encode("idna").decode("ascii")
        except UnicodeError:
            return host


def _is_globally_routable(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if not address.is_global:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        # Do not allow public-looking tunnel addresses to route to a private IPv4 host.
        if address.ipv4_mapped or address.sixtofour or address.teredo:
            return False
    return True


def resolve_public_addresses(host: str) -> tuple[str, ...]:
    """Resolve a host and reject it if any returned address is not globally routable."""
    if not host or "%" in host:
        raise UnsafeDestination("invalid host or IPv6 zone identifier")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        try:
            records = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise UnsafeDestination(f"could not resolve host: {host}") from exc
        addresses = {ipaddress.ip_address(record[4][0]) for record in records}
    else:
        addresses = {literal}

    if not addresses:
        raise UnsafeDestination(f"host resolved to no addresses: {host}")
    if any(not _is_globally_routable(address) for address in addresses):
        raise UnsafeDestination(f"host resolves to a non-public address: {host}")

    return tuple(sorted(address.compressed for address in addresses))


def _split_http_url(url: str) -> tuple[str, int]:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise UnsafeDestination("malformed URL") from exc

    if parsed.scheme.lower() not in ("http", "https") or not host:
        raise UnsafeDestination("only absolute HTTP(S) URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeDestination("URLs containing credentials are not allowed")

    host = _host_key(host)
    if not host:
        raise UnsafeDestination("URL has no host")
    if port == 0:
        raise UnsafeDestination("port zero is not a valid crawl destination")
    return host, port if port is not None else (443 if parsed.scheme.lower() == "https" else 80)


async def validate_url(url: str) -> tuple[str, int, tuple[str, ...]]:
    """Return host, port, and validated IPs without blocking the event loop on DNS."""
    host, port = _split_http_url(url)
    addresses = await asyncio.to_thread(resolve_public_addresses, host)
    return host, port, addresses


def validate_url_sync(url: str) -> tuple[str, int, tuple[str, ...]]:
    """Synchronous URL validation for the compliance gate."""
    host, port = _split_http_url(url)
    addresses = resolve_public_addresses(host)
    return host, port, addresses


@contextmanager
def pin_destination(host: str, port: int, address: str) -> Iterator[None]:
    """Pin the next transport connection for this origin to a checked address."""
    pins = dict(_PINNED_DESTINATIONS.get())
    pins[(_host_key(host), port)] = address
    token = _PINNED_DESTINATIONS.set(pins)
    try:
        yield
    finally:
        _PINNED_DESTINATIONS.reset(token)


class _PinnedAsyncNetworkBackend(httpcore.AsyncNetworkBackend):
    """Keep DNS validation and the TCP destination in the same request context."""

    def __init__(self, backend: httpcore.AsyncNetworkBackend):
        self._backend = backend

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ) -> httpcore.AsyncNetworkStream:
        address = _PINNED_DESTINATIONS.get().get((_host_key(host), port))
        if address is None:
            raise httpcore.ConnectError(f"refusing unvalidated destination: {host}")
        return await self._backend.connect_tcp(
            host=address,
            port=port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options=None
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("Unix socket connections are disabled for crawl requests")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


def create_async_client(
    *, timeout: float = 15.0, headers: Mapping[str, str] | None = None
) -> httpx.AsyncClient:
    """Create an HTTP client that can only connect through a validated DNS pin."""
    transport = httpx.AsyncHTTPTransport(trust_env=False)
    # httpx currently does not expose httpcore's network_backend parameter. Replace
    # the pool backend so origin Host headers and TLS SNI remain the original hostname
    # while TCP connects to the IP address that was just validated.
    pool = transport._pool  # type: ignore[attr-defined]
    pool._network_backend = _PinnedAsyncNetworkBackend(pool._network_backend)
    return httpx.AsyncClient(
        transport=transport,
        trust_env=False,
        follow_redirects=False,
        timeout=timeout,
        headers=headers,
    )


async def safe_request(
    client: httpx.AsyncClient,
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    content: bytes | None = None,
    follow_redirects: bool = True,
    max_redirects: int = 5,
) -> httpx.Response:
    """Send a request only after validating and pinning every destination hop."""
    current_url = url
    request_headers = dict(headers or {})
    original_origin: tuple[str, str, int] | None = None

    for redirect_count in range(max_redirects + 1):
        host, port, addresses = await validate_url(current_url)
        scheme = urlsplit(current_url).scheme.lower()
        origin = (scheme, host, port)
        if original_origin is None:
            original_origin = origin
        elif origin != original_origin:
            request_headers = {
                key: value for key, value in request_headers.items()
                if key.lower() not in {"authorization", "cookie", "proxy-authorization"}
            }

        response = None
        connection_error = None
        for address in addresses:
            with pin_destination(host, port, address):
                try:
                    response = await client.request(
                        method,
                        current_url,
                        headers=request_headers,
                        content=content,
                        follow_redirects=False,
                    )
                    break
                except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                    connection_error = exc
        if response is None:
            assert connection_error is not None
            raise connection_error

        location = response.headers.get("location")
        if not follow_redirects or response.status_code not in (301, 302, 303, 307, 308) or not location:
            return response
        if redirect_count >= max_redirects:
            request = response.request
            await response.aclose()
            raise httpx.TooManyRedirects("maximum redirect count exceeded", request=request)

        next_url = urljoin(str(response.url), location)
        await response.aclose()
        current_url = next_url
        if response.status_code == 303 or (
            response.status_code in (301, 302) and method.upper() not in ("GET", "HEAD")
        ):
            content = None
            method = "GET"

    raise AssertionError("redirect loop exited unexpectedly")


def _run_in_fresh_loop(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    # The compliance gate is synchronous and may be called by integrations that
    # already own an event loop. Run this isolated outbound request on a worker loop.
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()


def safe_get_sync(url: str, *, timeout: float = 10.0) -> httpx.Response:
    """Synchronous wrapper used by robots.txt checks."""
    async def get() -> httpx.Response:
        async with create_async_client(timeout=timeout) as client:
            return await safe_request(client, url)

    return _run_in_fresh_loop(get())
