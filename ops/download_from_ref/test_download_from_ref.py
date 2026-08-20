# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import ipaddress
import socket
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Iterator, List, Tuple
from unittest.mock import Mock

import download_from_ref
import pytest
from download_from_ref import CallbackBuilder, PinnedAddressAdapter, pinned_session

from vibe_core.data import ExternalReference
from vibe_core.file_downloader import download_file

HITS: List[Tuple[str, str, str]] = []


def make_ref(url: str) -> ExternalReference:
    now = datetime.now()
    return ExternalReference(
        id="test_id",
        time_range=(now, now),
        geometry={"type": "Point", "coordinates": [0.0, 0.0]},
        assets=[],
        url=url,
    )


def make_handler(name: str, redirect_to: str = "", body: bytes = b""):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            HITS.append((name, self.path, self.headers["Host"]))
            if redirect_to:
                self.send_response(302)
                self.send_header("Location", redirect_to)
            else:
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any):
            pass

    return Handler


@pytest.fixture
def redirect_to_internal() -> Iterator[str]:
    """Serve a url that redirects into another (internal) host, recording every request."""
    internal = HTTPServer(("127.0.0.1", 0), make_handler("internal"))
    attacker = HTTPServer(
        ("127.0.0.1", 0),
        make_handler("attacker", f"http://127.0.0.1:{internal.server_port}/x"),
    )
    for server in (internal, attacker):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    HITS.clear()
    yield f"http://attacker.test:{attacker.server_port}/redirect"
    for server in (internal, attacker):
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",  # arbitrary local file read
        "/etc/passwd",
        "ftp://example.com/raster.tif",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata service
        "http://127.0.0.1:8080/secret",
        "http://10.0.0.5/secret",
        "http://[::1]/secret",
    ],
)
def test_op_refuses_unsafe_refs(url: str):
    with pytest.raises(ValueError):
        CallbackBuilder("Raster")()(make_ref(url))


def test_redirect_into_internal_host_is_not_issued(
    redirect_to_internal: str, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
):
    original_resolver = download_from_ref.resolve_public_address

    def resolve_attacker(url: str) -> str:
        if download_from_ref.urlparse(url).hostname == "attacker.test":
            return "127.0.0.1"
        return original_resolver(url)

    monkeypatch.setattr(download_from_ref, "resolve_public_address", resolve_attacker)
    with pytest.raises(ValueError):
        with pinned_session() as session:
            download_file(redirect_to_internal, str(tmp_path / "out"), session=session)
    assert [hit[0] for hit in HITS] == ["attacker"]


def test_dns_rebinding_cannot_change_connection(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
):
    safe = HTTPServer(("127.0.0.1", 0), make_handler("safe", body=b"safe"))
    victim = HTTPServer(("127.0.0.2", safe.server_port), make_handler("victim", body=b"victim"))
    for server in (safe, victim):
        threading.Thread(target=server.serve_forever, daemon=True).start()

    real_getaddrinfo = socket.getaddrinfo
    real_ip_address = ipaddress.ip_address
    dns_calls = 0

    def rebind(host: str, *args: Any, **kwargs: Any):
        nonlocal dns_calls
        if host == "rebind.test":
            address = "127.0.0.1" if dns_calls == 0 else "127.0.0.2"
            dns_calls += 1
            port = args[0] if args else 0
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]
        return real_getaddrinfo(host, *args, **kwargs)

    def allow_test_loopback(address: str):
        parsed = real_ip_address(address)
        if not str(parsed).startswith("127."):
            return parsed

        class PublicLoopback:
            is_global = True
            is_multicast = False

            def __str__(self):
                return str(parsed)

        return PublicLoopback()

    monkeypatch.setattr(download_from_ref.socket, "getaddrinfo", rebind)
    monkeypatch.setattr(download_from_ref.ipaddress, "ip_address", allow_test_loopback)
    HITS.clear()
    try:
        url = f"http://rebind.test:{safe.server_port}/asset.tif"
        with pinned_session() as session:
            download_file(url, str(tmp_path / "out"), session=session)
        assert (tmp_path / "out").read_bytes() == b"safe"
        assert dns_calls == 1
        assert HITS == [("safe", "/asset.tif", f"rebind.test:{safe.server_port}")]
    finally:
        for server in (safe, victim):
            server.shutdown()
            server.server_close()


def test_https_pinning_preserves_tls_hostname(monkeypatch: pytest.MonkeyPatch):
    adapter = PinnedAddressAdapter()
    adapter.poolmanager = Mock()
    monkeypatch.setattr(download_from_ref, "resolve_public_address", lambda url: "93.184.216.34")

    adapter.connection_for_url("https://example.com/file")

    adapter.poolmanager.connection_from_host.assert_called_once_with(
        "93.184.216.34",
        port=None,
        scheme="https",
        pool_kwargs={"assert_hostname": "example.com", "server_hostname": "example.com"},
    )
