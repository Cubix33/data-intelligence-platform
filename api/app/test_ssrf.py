import asyncio
import ipaddress
import socket

import httpx
import pytest
import httpcore

from app import compliance, config, fetcher, ssrf


def test_fetch_chunks_never_connects_to_loopback(monkeypatch):
    """Untrusted crawl targets must be blocked before connecting to loopback."""
    async def run():
        requests = []

        async def respond(reader, writer):
            requests.append(True)
            await reader.read(4096)
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/html\r\n"
                b"Content-Length: 23\r\n"
                b"Connection: close\r\n\r\n"
                b"Sensitive local content"
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(respond, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(config, "DOMAIN_REQUEST_DELAY_S", 0)
        fetcher._page_cache.clear()

        try:
            chunks = await fetcher.fetch_chunks(f"http://127.0.0.1:{port}/secret")
        finally:
            server.close()
            await server.wait_closed()

        return requests, chunks

    requests, chunks = asyncio.run(run())

    assert requests == []
    assert chunks == []


@pytest.mark.parametrize("address", [
    "127.0.0.1",
    "10.1.2.3",
    "172.16.0.1",
    "192.168.1.1",
    "169.254.169.254",
    "::1",
    "fc00::1",
    "fe80::1",
    "::ffff:127.0.0.1",
    "2002:7f00:1::",
])
def test_resolver_rejects_non_public_ip_literals(address):
    with pytest.raises(ssrf.UnsafeDestination):
        ssrf.resolve_public_addresses(address)


@pytest.mark.parametrize("address", ["8.8.8.8", "2606:4700:4700::1111"])
def test_resolver_accepts_public_ip_literals(address):
    assert ssrf.resolve_public_addresses(address) == (ipaddress.ip_address(address).compressed,)


def test_resolver_rejects_mixed_public_and_private_dns_answers(monkeypatch):
    def mixed_answers(host, port, type):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 0)),
        ]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", mixed_answers)
    with pytest.raises(ssrf.UnsafeDestination):
        ssrf.resolve_public_addresses("mixed.example")


def test_transport_connects_to_the_validated_ip_pin():
    class RecordingBackend:
        def __init__(self):
            self.hosts = []

        async def connect_tcp(self, host, port, **kwargs):
            self.hosts.append((host, port))
            return object()

        async def connect_unix_socket(self, path, **kwargs):
            raise AssertionError("unexpected Unix socket connection")

        async def sleep(self, seconds):
            pass

    async def run():
        backend = RecordingBackend()
        pinned = ssrf._PinnedAsyncNetworkBackend(backend)
        with ssrf.pin_destination("www.example", 443, "93.184.216.34"):
            await pinned.connect_tcp("www.example", 443)
        return backend.hosts

    assert asyncio.run(run()) == [("93.184.216.34", 443)]


def test_transport_refuses_connections_without_a_validated_pin():
    class RecordingBackend:
        async def connect_tcp(self, host, port, **kwargs):
            raise AssertionError("unvalidated address reached the socket backend")

        async def connect_unix_socket(self, path, **kwargs):
            raise AssertionError("unexpected Unix socket connection")

        async def sleep(self, seconds):
            pass

    async def run():
        backend = ssrf._PinnedAsyncNetworkBackend(RecordingBackend())
        with pytest.raises(httpcore.ConnectError):
            await backend.connect_tcp("www.example", 443)

    asyncio.run(run())


def test_safe_request_revalidates_redirect_before_connecting(monkeypatch):
    requests = []
    real_resolver = ssrf.resolve_public_addresses

    def resolve(host):
        if host == "public.example":
            return ("93.184.216.34",)
        return real_resolver(host)

    monkeypatch.setattr(ssrf, "resolve_public_addresses", resolve)

    async def run():
        def respond(request):
            requests.append(str(request.url))
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(ssrf.UnsafeDestination):
                await ssrf.safe_request(client, "https://public.example/start")

    asyncio.run(run())
    assert requests == ["https://public.example/start"]


def test_safe_request_checks_and_allows_each_public_redirect(monkeypatch):
    requests = []
    monkeypatch.setattr(ssrf, "resolve_public_addresses", lambda host: ("93.184.216.34",))

    async def run():
        def respond(request):
            requests.append(str(request.url))
            if request.url.path == "/start":
                return httpx.Response(302, headers={"Location": "/final"})
            return httpx.Response(200, text="ok")

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            response = await ssrf.safe_request(client, "https://public.example/start")
            assert response.text == "ok"

    asyncio.run(run())
    assert requests == [
        "https://public.example/start",
        "https://public.example/final",
    ]


def test_safe_request_retries_other_validated_addresses_on_connect_failure(monkeypatch):
    requests = []
    monkeypatch.setattr(
        ssrf,
        "resolve_public_addresses",
        lambda host: ("93.184.216.34", "8.8.8.8"),
    )

    async def run():
        def respond(request):
            requests.append(str(request.url))
            if len(requests) == 1:
                raise httpx.ConnectError("first address unavailable", request=request)
            return httpx.Response(200, text="ok")

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            response = await ssrf.safe_request(client, "https://public.example/")
            assert response.text == "ok"

    asyncio.run(run())
    assert requests == ["https://public.example/", "https://public.example/"]


def test_pagination_target_is_validated_before_fetch(monkeypatch):
    requested = []
    real_resolver = ssrf.resolve_public_addresses
    real_client = httpx.AsyncClient

    def resolve(host):
        if host == "public.example":
            return ("93.184.216.34",)
        return real_resolver(host)

    def client_factory(**kwargs):
        def respond(request):
            requested.append(str(request.url))
            html = (
                "<html><body>" + ("ordinary public page content " * 20)
                + '<a rel="next" href="http://127.0.0.1/private">Next</a></body></html>'
            )
            return httpx.Response(200, headers={"content-type": "text/html"}, text=html)

        return real_client(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(ssrf, "resolve_public_addresses", resolve)
    monkeypatch.setattr(ssrf, "create_async_client", client_factory)
    monkeypatch.setattr(config, "DOMAIN_REQUEST_DELAY_S", 0)
    monkeypatch.setattr(config, "MIN_TEXT_CHARS_FOR_JS_FALLBACK", 0)
    fetcher._page_cache.clear()

    chunks = asyncio.run(fetcher.fetch_chunks("https://public.example/start"))

    assert chunks
    assert requested == ["https://public.example/start"]


def test_compliance_rejects_internal_urls_before_reading_robots(monkeypatch):
    monkeypatch.setattr(
        ssrf,
        "safe_get_sync",
        lambda url: pytest.fail(f"robots.txt must not be requested for {url}"),
    )
    compliance._parser_for.cache_clear()

    allowed, reason = compliance.is_allowed("http://127.0.0.1:8000/private")

    assert not allowed
    assert "unsafe destination" in reason


def test_robots_parser_uses_safe_http_loader(monkeypatch):
    requests = []

    class Response:
        status_code = 200
        text = "User-agent: *\nDisallow: /private"

    def safe_get(url):
        requests.append(url)
        return Response()

    monkeypatch.setattr(ssrf, "safe_get_sync", safe_get)
    compliance._parser_for.cache_clear()

    parser = compliance._parser_for("public.example", "https")

    assert requests == ["https://public.example/robots.txt"]
    assert not parser.can_fetch(config.USER_AGENT, "https://public.example/private")
    compliance._parser_for.cache_clear()


def test_robots_request_revalidates_redirect_before_connecting(monkeypatch):
    requests = []
    real_resolver = ssrf.resolve_public_addresses
    real_client = httpx.AsyncClient

    def resolve(host):
        if host == "public.example":
            return ("93.184.216.34",)
        return real_resolver(host)

    def client_factory(**kwargs):
        def respond(request):
            requests.append(str(request.url))
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})

        return real_client(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(ssrf, "resolve_public_addresses", resolve)
    monkeypatch.setattr(ssrf, "create_async_client", client_factory)

    with pytest.raises(ssrf.UnsafeDestination):
        ssrf.safe_get_sync("https://public.example/robots.txt")

    assert requests == ["https://public.example/robots.txt"]


def test_playwright_http_resources_use_safe_request(monkeypatch):
    requests = []

    class Request:
        url = "http://169.254.169.254/latest/meta-data/"
        method = "GET"
        headers = {}
        post_data_buffer = None

    class Route:
        request = Request()
        aborted = False

        async def abort(self, reason):
            self.aborted = True

        async def continue_(self):
            raise AssertionError("unsafe HTTP requests must not continue")

        async def fulfill(self, **kwargs):
            raise AssertionError("unsafe HTTP requests must not be fulfilled")

    real_resolver = ssrf.resolve_public_addresses

    def resolve(host):
        if host == "public.example":
            return ("93.184.216.34",)
        return real_resolver(host)

    monkeypatch.setattr(ssrf, "resolve_public_addresses", resolve)

    async def run():
        def respond(request):
            requests.append(str(request.url))
            return httpx.Response(200)

        route = Route()
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await fetcher._guard_playwright_request(route, client)
        return route

    route = asyncio.run(run())

    assert route.aborted
    assert requests == []


def test_playwright_websocket_connections_are_closed():
    class WebSocketRoute:
        closed_with = None

        async def close(self, *, code, reason):
            self.closed_with = (code, reason)

    web_socket_route = WebSocketRoute()
    asyncio.run(fetcher._block_playwright_websocket(web_socket_route))

    assert web_socket_route.closed_with == (1008, "websocket connections are disabled")
