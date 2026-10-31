"""Narrow unit coverage for the remote-import policy before network I/O."""
from __future__ import annotations

import pytest
import asyncio
import io
import importlib.util
import json
from pathlib import Path
import socket
import ssl
import time
import threading
import notesbot.remote_pdf as remote_pdf
import aiohappyeyeballs

from notesbot import blob_store
from notesbot.blob_store import BlobRef, BlobStore
from notesbot.remote_pdf import (
    MAX_INPUT_BYTES,
    RemotePdfImportError,
    _confirmation_url,
    _drive_download_url,
    _drive_file,
    _safe_url,
    _PinnedResolver,
    _filename,
)


STORE_ID = "store_4mlmgDlkyHHkSimU"
INPUT_PATH = "shortnotes/v1/inputs/" + "b" * 32 + "/" + "a" * 32 + ".pdf"
PDF_BYTES = b"%PDF-1.7\n%%EOF"

# A self-signed test CA/certificate whose SANs cover only the two controlled
# HTTPS authorities exercised below.  Tests install this CA only for their
# local socket boundary; production TLS and hostname verification stay enabled.
_TEST_CERT = """-----BEGIN CERTIFICATE-----
MIIC/jCCAeagAwIBAgIUMAEaJ4PEXOcw5EgPPI50zlIbsRcwDQYJKoZIhvcNAQEL
BQAwGzEZMBcGA1UEAwwQbm90ZXNib3QgdGVzdCBDQTAeFw0yNjA4MDEwOTE5MDNa
Fw0zNjA3MzAwOTE5MDNaMBsxGTAXBgNVBAMMEG5vdGVzYm90IHRlc3QgQ0EwggEi
MA0GCSqGSIb3DQEBAQUAA4IBDwAwggEKAoIBAQCbdAXoO0Yq0Gv6x3IzEHGTPUdX
aannlzq8muukk/RbkEFTMAHsFkorcwV3J1jr/iWXdEFVYS6LHJtQozPy/c6PX6JZ
1o9C8rVlN1ddrVRIDRkMY1h6nKJvt5B7yFTiruJ9XtonRD3NskQ2INTVt4rgGvk6
dkZqlwxEL6ozLaz+4QVORDZSSdcI8/h+h/8ORwS7TdsqD/m+nsY1MZgefCzwHh94
gqAXdgLOI1Pf+w9jA1oW08QZd8KcXFmmpnPTdA4lKhZ359lrRDk4dkhGX806kZxQ
/FAWHWnDa3ZfhJkRfiBOCuFiEaO2O6ymwiJxMYyex18dS7yONpbNjDFJePfBAgMB
AAGjOjA4MA8GA1UdEwEB/wQFMAMBAf8wJQYDVR0RBB4wHIIOcHVibGljLmV4YW1w
bGWCCnZlcmNlbC5jb20wDQYJKoZIhvcNAQELBQADggEBAG47NfXLDk+NLbISZOiK
oxEamkBOTGLwu2ZKpDTfxEXPzU5Dr8zsM0y36aGOCUjnYFGYAUQEDKTGJa/0GUlD
mkZUwKI8v6esupI2ZNML23B7zemV8Dr7m1bQCO5IXrDKNCvP3bIbpm5bEng371/q
8DtUTydDXk4k14yvHM5J8TNBk8GxMAZqNnzx8ZJCD1IdlEQwVSYqasjIICWK8CwU
BgJr2ctTff/Cwyagz39OAhva7vwy3DzGjaZ8ibqlA4wpnvIMdGT+4vLzZSz5+CPn
CltAHNs7mnNUOE+9jK2ZlgZWhZZwmbwDM1Pc7jVT3WuKQQezM84wV/B9avc+KArq
uKk=
-----END CERTIFICATE-----
"""
_TEST_KEY = """-----BEGIN RSA PRIVATE KEY-----
MIIEogIBAAKCAQEAm3QF6DtGKtBr+sdyMxBxkz1HV2mp55c6vJrrpJP0W5BBUzAB
7BZKK3MFdydY6/4ll3RBVWEuixybUKMz8v3Oj1+iWdaPQvK1ZTdXXa1USA0ZDGNY
epyib7eQe8hU4q7ifV7aJ0Q9zbJENiDU1beK4Br5OnZGapcMRC+qMy2s/uEFTkQ2
UknXCPP4fof/DkcEu03bKg/5vp7GNTGYHnws8B4feIKgF3YCziNT3/sPYwNaFtPE
GXfCnFxZpqZz03QOJSoWd+fZa0Q5OHZIRl/NOpGcUPxQFh1pw2t2X4SZEX4gTgrh
YhGjtjuspsIicTGMnsdfHUu8jjaWzYwxSXj3wQIDAQABAoIBACZRMOuPQLpo6VTF
OJbxpK0gW6dmOiUFl8rJfO/yMDcplZCFstOYouuWzHAUWMzCtk1gsIH1FF3hzT7R
G4r3mKSqdAAk7OcZoDC0tWo3wSCJafUtv69pUHitDIYWWSYO6XDxomAvZ/R9zmZx
LUUImsimnWwVn0HCvZlJDnDS/2NLfYUaYTKUfm7gPW32UIQL7C/82dzicXyu+65c
N7avcamW9ueb2uH0I2qE88KJngY7qw3YIFH4aAbw04Dcbcat3YLi4H8WuaCC3Fsg
CdRlBzUoyblv9tRkI6J+jRXOY5vU/ttbUKzwgRR/1ITuaIF07dHKRiFxwfVkVd/j
d9SIkokCgYEAx72iLhUUyeCaxuW9B9Y1ZEYcu5g2lAZikC96N8Gj/ELU5uImz3Dr
x6DIv9KqodmgzaVJ1vRzXPUlvbiRnENrJRZnVwupFp4dpJqzyxObMqdcYzLTnqwG
XTxdkS5JCqKK73CyCwok5wwcqprD7sb4sBPvA9+8Mo/8AGQV4Fo8mzsCgYEAxz0F
9ihjd+qIWdihZ/PCYr/JCh3PJ2vXy7I7w9a5kTwoJ8ep94sTctKzUdlwfocLXfyI
uCYtqfdTGIcbC02LZTn8iZLcp12bxlBq9tYsaTkZaaOhmt+ps3RqKnjGxRJsQS6U
Exk+w14yhBRAKvuPboyulW/UpLmzIpA8SYSTcTMCgYAb0q2Jr8QSjufgdtYLBmqv
ihishPptSipmnUZwB9g45nJTqwLu+HLPfvUXy1ebbf45RyHvAGQlebyFHfWih72g
D+vbMzOdg+7rI+WRjyyaNLfo1Yam3p2QVT1PYNwp0BR8X/1dDk+vo39GwII0e6De
nhg4OO01OdtHjqmBc/BnSwKBgCZA4i8kDA0PG8TcYgkadT0Og6QMmyR/gTxb6Tut
s41eFnuky057Bm3qZIGXlRDGDvmprfplYikbEnpIHnoqFlSGMKWda/46a9lEwXTJ
JFfDncx5nWKmBOgutt+WtttJsT1GmymblEBgiqRvWu6lxCE+o5qPkS1bdjhxpxGa
gdCfAoGAOGEH5Qdf2Yas82RyDeepI+HU9HYtxWEOSA9471mZ7/d5cgQEv2NL/N1A
X4upD3pt0jmlk6GA6rEySzv5iLRPko68qbOv7X14X6oZ3kEGnQELk97PTvc1qP5Y
fM9/zBFdHuIR7cpXdNqZnx7w5zovfVO4plP24TwUL/yuwOVjeSY=
-----END RSA PRIVATE KEY-----
"""


class _TrackedSpool:
    def __init__(self, wrapped, closed):
        self.wrapped = wrapped
        self.closed_event = closed

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        self.wrapped.close()
        self.closed_event.set()

    def __getattr__(self, name):
        return getattr(self.wrapped, name)


class _TlsScenarioServer:
    """A controlled HTTPS peer; stalls are released only by client closure."""

    def __init__(self, phase, cert_path, key_path, entered):
        self.phase = phase
        self.entered = entered
        self.peer_closed = threading.Event()
        self.connection_count = 0
        self.request_count = 0
        self._stop = threading.Event()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(4)
        self._listener.settimeout(0.05)
        self.port = self._listener.getsockname()[1]
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._context.load_cert_chain(cert_path, key_path)
        self._thread = threading.Thread(target=self._serve, name="remote-pdf-test-peer")
        self._thread.start()

    def close(self):
        self._stop.set()
        self._listener.close()
        self._thread.join(2)
        assert not self._thread.is_alive()

    def _serve(self):
        while not self._stop.is_set():
            try:
                raw, _ = self._listener.accept()
            except (OSError, socket.timeout):
                continue
            self.connection_count += 1
            try:
                with self._context.wrap_socket(raw, server_side=True) as stream:
                    stream.settimeout(0.05)
                    request = self._read_request(stream)
                    if not request:
                        continue
                    method = request.split(b" ", 1)[0]
                    if method == b"GET":
                        self._handle_get(stream)
                    elif method == b"PUT":
                        self._handle_put(stream)
            except (ConnectionError, OSError, ssl.SSLError):
                self.peer_closed.set()

    def _read_request(self, stream):
        data = bytearray()
        while b"\r\n\r\n" not in data and not self._stop.is_set():
            try:
                chunk = stream.recv(64 * 1024)
            except socket.timeout:
                continue
            if not chunk:
                self.peer_closed.set()
                return b""
            data.extend(chunk)
        head, _, body = bytes(data).partition(b"\r\n\r\n")
        content_length = 0
        for line in head.split(b"\r\n")[1:]:
            name, _, value = line.partition(b":")
            if name.lower() == b"content-length":
                content_length = int(value.strip())
        while len(body) < content_length and not self._stop.is_set():
            try:
                chunk = stream.recv(content_length - len(body))
            except socket.timeout:
                continue
            if not chunk:
                self.peer_closed.set()
                break
            body += chunk
        return head + b"\r\n\r\n" + body

    def _wait_for_client_close(self, stream):
        while not self._stop.is_set():
            try:
                if not stream.recv(1):
                    self.peer_closed.set()
                    return
            except socket.timeout:
                continue
            except (ConnectionError, OSError, ssl.SSLError):
                self.peer_closed.set()
                return

    def _handle_get(self, stream):
        self.request_count += 1
        if self.phase == "redirect" and self.request_count == 1:
            stream.sendall(
                b"HTTP/1.1 302 Found\r\n"
                b"Location: https://public.example/final.pdf\r\n"
                b"Content-Length: 0\r\nConnection: keep-alive\r\n\r\n"
            )
            return
        if self.phase == "redirect":
            self.entered.set()
        if self.phase == "pinning":
            self.entered.set()
        if self.phase == "headers":
            self.entered.set()
            self._wait_for_client_close(stream)
            return
        if self.phase == "body":
            stream.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/pdf\r\n"
                b"Content-Length: 14\r\nConnection: close\r\n\r\n%PDF"
            )
            self.entered.set()
            self._wait_for_client_close(stream)
            return
        stream.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/pdf\r\n"
            b"Content-Length: 14\r\nConnection: close\r\n\r\n" + PDF_BYTES
        )

    def _handle_put(self, stream):
        assert self.phase == "blob_response"
        body = json.dumps({
            "url": "https://4mlmgdlkyhhksimu.private.blob.vercel-storage.com/" + INPUT_PATH,
            "pathname": INPUT_PATH,
            "contentType": "application/pdf",
            "etag": '"etag"',
        }).encode()
        stream.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
        )
        self.entered.set()
        self._wait_for_client_close(stream)


class _PublicResolver:
    def __init__(self):
        self.calls = []
        self.closed = 0

    async def resolve(self, host, port=0, family=0):
        self.calls.append((host, port, family))
        return [{
            "hostname": host,
            "host": "8.8.8.8",
            "port": port,
            "family": socket.AF_INET,
            "proto": socket.IPPROTO_TCP,
            "flags": socket.AI_NUMERICHOST,
        }]

    async def close(self):
        self.closed += 1


class _ScriptedContent:
    def __init__(self, chunks=(), read_bytes=None):
        self.chunks = list(chunks)
        self.read_bytes = read_bytes
        self.iterations = 0

    async def iter_chunked(self, _size):
        self.iterations += 1
        for chunk in self.chunks:
            yield chunk

    async def read(self, limit):
        value = self.read_bytes
        if value is None:
            value = b"".join(self.chunks)
        return value[:limit]


class _ScriptedResponse:
    def __init__(self, status=200, headers=None, chunks=(), read_bytes=None):
        self.status = status
        self.headers = headers or {}
        self.content = _ScriptedContent(chunks, read_bytes)


def _install_scripted_session(monkeypatch, responses, calls):
    queue = list(responses)

    class Request:
        def __init__(self, response):
            self.response = response

        async def __aenter__(self):
            return self.response

        async def __aexit__(self, *_args):
            return None

    class Session:
        def __init__(self, *args, **kwargs):
            calls.append(("session", kwargs))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def get(self, url, **kwargs):
            calls.append(("GET", url, kwargs))
            if not queue:
                raise AssertionError("unexpected extra GET")
            return Request(queue.pop(0))

    monkeypatch.setattr(remote_pdf.aiohttp, "ClientSession", Session)
    return queue


def rejected(fn, *args):
    with pytest.raises(RemotePdfImportError):
        fn(*args)


def test_drive_file_share_view_open_and_uc_links_extract_one_exact_file_id():
    file_id = "A" * 20
    for url in (
        f"https://drive.google.com/file/d/{file_id}/view?usp=sharing",
        f"https://drive.google.com/open?id={file_id}&usp=drive_link",
        f"https://drive.google.com/uc?id={file_id}&export=download",
    ):
        assert _drive_file(_safe_url(url)).file_id == file_id


def test_drive_single_resourcekey_is_preserved_through_download_and_confirmation():
    info = _drive_file(_safe_url("https://drive.google.com/open?id=" + "A" * 20 + "&resourcekey=key_123"))
    assert "resourcekey=key_123" in _drive_download_url(info)
    assert "resourcekey=key_123" in _confirmation_url(
        (f'<form action="https://drive.usercontent.google.com/download"><input name="id" value="{info.file_id}"><input name="export" value="download"><input name="confirm" value="yes"><input name="resourcekey" value="key_123"></form>').encode(), info
    )


def test_drive_duplicate_unknown_query_keys_or_resourcekey_mismatch_are_rejected():
    base = "https://drive.google.com/open?id=" + "A" * 20
    rejected(_drive_file, _safe_url(base + "&id=" + "B" * 20))
    rejected(_drive_file, _safe_url(base + "&evil=x"))
    info = _drive_file(_safe_url(base + "&resourcekey=key_123"))
    rejected(_confirmation_url, b'<form action="https://drive.usercontent.google.com/download"><input name="id" value="AAAAAAAAAAAAAAAAAAAA"><input name="export" value="download"><input name="confirm" value="yes"><input name="resourcekey" value="other"></form>', info)


def test_drive_folder_docs_sheets_slides_and_malformed_ids_are_rejected_before_dns():
    for path in ("/drive/folders/abc", "/document/d/abc/edit", "/file/d/short/view"):
        rejected(_drive_file, _safe_url("https://drive.google.com" + path))


def test_drive_confirmation_rejects_unknown_fields_action_host_path_or_second_confirmation():
    info = _drive_file(_safe_url("https://drive.google.com/open?id=" + "A" * 20))
    for action, extra in (("https://evil.example/download", ""), ("https://drive.usercontent.google.com/other", ""), ("https://drive.usercontent.google.com/download", '<input name="evil" value="x">')):
        html = f'<form action="{action}"><input name="id" value="{info.file_id}"><input name="export" value="download"><input name="confirm" value="yes">{extra}</form>'.encode()
        rejected(_confirmation_url, html, info)


def test_remote_link_rejects_http_userinfo_explicit_port_fragment_and_oversized_url():
    for value in ("http://example.com/a", "https://u@example.com/a", "https://example.com:444/a", "https://example.com/a#x", "https://example.com/" + "a" * 2049):
        rejected(_safe_url, value)


def test_remote_link_rejects_controls_backslashes_ambiguous_authority_and_encoded_host():
    for value in ("https://example.com/a\\b", "https://example.com/a\n", "https://example.com%2f.evil/a"):
        rejected(_safe_url, value)


@pytest.mark.parametrize(
    ("url", "code"),
    (
        ("https://127\u30020\u30020\u30021/a.pdf", "private_destination"),
        ("https://127\uff0e0\uff0e0\uff0e1/a.pdf", "private_destination"),
        ("https://127\uff610\uff610\uff611/a.pdf", "private_destination"),
        ("https://2130706433/a.pdf", "private_destination"),
        ("https://0177.0.0.1/a.pdf", "private_destination"),
        ("https://127.1/a.pdf", "private_destination"),
        ("https://127.0.1/a.pdf", "private_destination"),
        ("https://0300.0250.0001.0001/a.pdf", "private_destination"),
        ("https://localhost\u3002/a.pdf", "invalid_link"),
    ),
)
def test_remote_link_rejects_idna_canonicalized_ip_literals_and_trailing_dot_before_dns(
    url, code
):
    with pytest.raises(RemotePdfImportError) as caught:
        _safe_url(url)
    assert caught.value.code == code


def test_remote_link_retains_the_validated_ascii_idna_host_in_canonical_url():
    assert _safe_url("https://b\u00fccher.example/a.pdf?token=opaque") == (
        "https://xn--bcher-kva.example/a.pdf?token=opaque"
    )


def test_remote_python_browser_and_blob_input_limits_are_exactly_100_mib():
    root = Path(__file__).resolve().parents[1]
    expected = 100 * 1024 * 1024
    assert MAX_INPUT_BYTES == expected
    # The browser accepts local files and the upload endpoint independently;
    # read their shipped source rather than mirroring a test-only constant.
    assert "MAX_INPUT_BYTES = 100 * 1024 * 1024" in (root / "web" / "state.js").read_text(encoding="utf-8")
    assert "MAX_UPLOAD_BYTES = 100 * 1024 * 1024" in (root / "api" / "blob-upload.js").read_text(encoding="utf-8")


def test_remote_resolver_rejects_literal_or_resolved_non_global_ipv4_ipv6_and_mixed_answers():
    class Resolver:
        async def resolve(self, host, port, family): return [{"host": "8.8.8.8"}, {"host": "127.0.0.1"}]
        async def close(self): pass
    with pytest.raises(RemotePdfImportError) as error:
        asyncio.run(_PinnedResolver(Resolver()).resolve("example.com", 443))
    assert error.value.code == "private_destination"


@pytest.mark.parametrize(
    ("address", "family"),
    (
        ("192.0.0.8", socket.AF_INET),
        ("192.31.196.1", socket.AF_INET),
        ("192.52.193.1", socket.AF_INET),
        ("192.175.48.1", socket.AF_INET),
        ("224.0.0.1", socket.AF_INET),
        ("239.255.255.250", socket.AF_INET),
        ("64:ff9b::a9fe:a9fe", socket.AF_INET6),
        ("64:ff9b:1::a9fe:a9fe", socket.AF_INET6),
        ("2001:0:4136:e378:8000:63bf:3fff:fdd2", socket.AF_INET6),
        ("2002:7f00:1::", socket.AF_INET6),
        ("2620:4f:8000::1", socket.AF_INET6),
        ("fec0::1", socket.AF_INET6),
        ("ff02::1", socket.AF_INET6),
        ("::ffff:169.254.169.254", socket.AF_INET6),
    ),
)
def test_remote_resolver_rejects_iana_special_multicast_and_ipv4_transition_answers(
    address, family
):
    class Resolver:
        async def resolve(self, host, port, requested_family):
            return [{
                "hostname": host,
                "host": address,
                "port": port,
                "family": family,
                "proto": socket.IPPROTO_TCP,
                "flags": 0,
            }]

        async def close(self):
            return None

    with pytest.raises(RemotePdfImportError) as caught:
        asyncio.run(_PinnedResolver(Resolver()).resolve("example.com", 443))
    assert caught.value.code == "private_destination"


@pytest.mark.parametrize(
    ("address", "family"),
    (
        ("8.8.8.8", socket.AF_INET),
        ("2606:4700:4700::1111", socket.AF_INET6),
    ),
)
def test_remote_resolver_retains_ordinary_public_unicast_answers(address, family):
    class Resolver:
        async def resolve(self, host, port, requested_family):
            return [{
                "hostname": host,
                "host": address,
                "port": port,
                "family": family,
                "proto": socket.IPPROTO_TCP,
                "flags": 0,
            }]

        async def close(self):
            return None

    rows = asyncio.run(_PinnedResolver(Resolver()).resolve("example.com", 443))
    assert rows == [{
        "hostname": "example.com",
        "host": address,
        "port": 443,
        "family": family,
        "proto": socket.IPPROTO_TCP,
        "flags": socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
    }]


def test_remote_connector_uses_the_validated_resolver_answers_without_second_dns_lookup(
    monkeypatch, tls_material
):
    os_lookups = []
    socket_destinations = []
    cert_path, key_path = tls_material
    reached = threading.Event()
    server = _TlsScenarioServer("pinning", cert_path, key_path, reached)

    class Resolver:
        def __init__(self): self.calls = 0
        async def resolve(self, host, port, family):
            self.calls += 1
            return [{
                "hostname": "untrusted.invalid",
                "host": "8.8.8.8",
                "port": 9,
                "family": socket.AF_INET,
                "proto": 0,
                "flags": 0,
            }]
        async def close(self): pass

    resolver = Resolver()

    def forbidden_os_lookup(*args, **kwargs):
        os_lookups.append((args, kwargs))
        raise AssertionError("connector performed a second OS lookup")

    original_start_connection = aiohappyeyeballs.start_connection

    async def observe_socket_boundary(*, addr_infos, **kwargs):
        socket_destinations.extend(address[-1] for address in addr_infos)
        redirected = [
            (family, kind, proto, canonname, ("127.0.0.1", server.port))
            for family, kind, proto, canonname, _address in addr_infos
        ]
        return await original_start_connection(addr_infos=redirected, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", forbidden_os_lookup)
    monkeypatch.setattr(aiohappyeyeballs, "start_connection", observe_socket_boundary)
    import aiohttp.connector as connector_module
    trusted_context = ssl.create_default_context(cafile=str(cert_path))
    trusted_context.set_alpn_protocols(("http/1.1",))
    monkeypatch.setattr(connector_module, "_SSL_CONTEXT_VERIFIED", trusted_context)

    async def exercise():
        loop = asyncio.get_running_loop()
        loop.run_in_executor = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("executor used")
        )
        metadata = await remote_pdf.fetch_public_pdf(
            "https://public.example/a.pdf",
            io.BytesIO(),
            deadline_at=time.monotonic() + 30,
            resolver=resolver,
        )
        assert getattr(loop, "_default_executor", None) is None
        return metadata

    try:
        metadata = asyncio.run(exercise())
    finally:
        server.close()

    assert metadata.size == len(PDF_BYTES)
    assert reached.is_set()
    assert resolver.calls == 1
    assert os_lookups == []
    assert socket_destinations == [("8.8.8.8", 443)]


def test_remote_same_host_redirect_force_closes_and_repins_each_real_tls_get(
    monkeypatch, tls_material
):
    cert_path, key_path = tls_material
    reached = threading.Event()
    server = _TlsScenarioServer("redirect", cert_path, key_path, reached)
    resolver = _PublicResolver()
    destinations = []
    connector_options = []
    original_connector = remote_pdf.aiohttp.TCPConnector
    original_start_connection = aiohappyeyeballs.start_connection

    def observed_connector(*args, **kwargs):
        connector_options.append(dict(kwargs))
        return original_connector(*args, **kwargs)

    async def controlled_connection(*, addr_infos, **kwargs):
        destinations.extend(address[-1] for address in addr_infos)
        redirected = [
            (family, kind, proto, canonname, ("127.0.0.1", server.port))
            for family, kind, proto, canonname, _address in addr_infos
        ]
        return await original_start_connection(addr_infos=redirected, **kwargs)

    def forbidden_lookup(*_args, **_kwargs):
        raise AssertionError("OS resolver used")

    monkeypatch.setattr(remote_pdf.aiohttp, "TCPConnector", observed_connector)
    monkeypatch.setattr(aiohappyeyeballs, "start_connection", controlled_connection)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden_lookup)
    import aiohttp.connector as connector_module
    trusted_context = ssl.create_default_context(cafile=str(cert_path))
    trusted_context.set_alpn_protocols(("http/1.1",))
    monkeypatch.setattr(connector_module, "_SSL_CONTEXT_VERIFIED", trusted_context)

    async def exercise():
        loop = asyncio.get_running_loop()
        loop.run_in_executor = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("executor used")
        )
        return await remote_pdf.fetch_public_pdf(
            "https://public.example/start.pdf",
            io.BytesIO(),
            deadline_at=time.monotonic() + 30,
            resolver=resolver,
        )

    try:
        metadata = asyncio.run(exercise())
    finally:
        server.close()

    assert metadata.size == len(PDF_BYTES)
    assert reached.is_set()
    assert server.request_count == 2
    assert server.connection_count == 2
    assert len(resolver.calls) == 2
    assert destinations == [("8.8.8.8", 443), ("8.8.8.8", 443)]
    assert connector_options[0]["use_dns_cache"] is False
    assert connector_options[0]["force_close"] is True


def test_drive_redirect_to_non_download_google_path_fails_before_a_second_get(monkeypatch):
    calls = []
    class Response:
        status = 302
        headers = {"Location": "https://drive.google.com/drive/folders/blocked"}
    class Request:
        async def __aenter__(self): return Response()
        async def __aexit__(self, *_args): pass
    class Session:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        def get(self, url, **_kwargs): calls.append(url); return Request()
    monkeypatch.setattr(remote_pdf.aiohttp, "ClientSession", Session)
    file_id = "A" * 20
    with pytest.raises(RemotePdfImportError, match="public"):
        asyncio.run(remote_pdf.fetch_public_pdf(f"https://drive.google.com/open?id={file_id}", io.BytesIO(), deadline_at=time.monotonic() + 30))
    assert len(calls) == 1


@pytest.mark.parametrize(("source_query", "redirect"), [
    (
        "&resourcekey=key_123",
        "https://drive.google.com/uc?id=" + "B" * 20
        + "&export=download&resourcekey=key_123",
    ),
    (
        "&resourcekey=key_123",
        "https://drive.usercontent.google.com/download?id=" + "A" * 20
        + "&export=download&resourcekey=other_key",
    ),
    (
        "&resourcekey=key_123",
        "https://drive.usercontent.google.com/download?id=" + "A" * 20
        + "&export=download",
    ),
    (
        "",
        "https://drive.usercontent.google.com/download?id=" + "A" * 20
        + "&export=download&resourcekey=injected_key",
    ),
])
def test_drive_redirect_cannot_switch_inject_or_drop_identity_before_second_get(
    monkeypatch, source_query, redirect
):
    calls = []
    class Response:
        status = 302
        headers = {"Location": redirect}
    class Request:
        async def __aenter__(self): return Response()
        async def __aexit__(self, *_args): pass
    class Session:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        def get(self, url, **_kwargs): calls.append(url); return Request()
    monkeypatch.setattr(remote_pdf.aiohttp, "ClientSession", Session)
    with pytest.raises(RemotePdfImportError):
        asyncio.run(remote_pdf.fetch_public_pdf(
            "https://drive.google.com/open?id=" + "A" * 20 + source_query,
            io.BytesIO(),
            deadline_at=time.monotonic() + 30,
        ))
    assert len(calls) == 1


@pytest.fixture
def tls_material(tmp_path):
    cert_path = tmp_path / "controlled-ca.pem"
    key_path = tmp_path / "controlled-key.pem"
    cert_path.write_text(_TEST_CERT, encoding="ascii")
    key_path.write_text(_TEST_KEY, encoding="ascii")
    return cert_path, key_path


def test_drive_confirmation_rejects_unknown_fields_action_host_path_or_second_confirmation_post_method():
    info = _drive_file(_safe_url("https://drive.google.com/open?id=" + "A" * 20))
    rejected(_confirmation_url, f'<form method="post" action="https://drive.usercontent.google.com/download"><input name="id" value="{info.file_id}"><input name="export" value="download"><input name="confirm" value="yes"></form>'.encode(), info)


def test_remote_sanitized_filename_is_bounded():
    assert _filename({}, "https://example.com/" + "a" * 100 + ".pdf") == "linked-lecture.pdf"


def test_direct_public_https_pdf_streams_once_to_bounded_spool(monkeypatch):
    calls = []
    resolver = _PublicResolver()
    response = _ScriptedResponse(
        headers={
            "Content-Type": "application/pdf",
            "Content-Length": str(len(PDF_BYTES)),
        },
        chunks=[PDF_BYTES[:7], PDF_BYTES[7:]],
    )
    remaining = _install_scripted_session(monkeypatch, [response], calls)

    sink = io.BytesIO()
    metadata = asyncio.run(remote_pdf.fetch_public_pdf(
        "https://public.example/lecture.pdf",
        sink,
        deadline_at=time.monotonic() + 30,
        resolver=resolver,
    ))

    assert metadata == remote_pdf.RemotePdfMetadata(len(PDF_BYTES), "lecture.pdf")
    assert sink.getvalue() == PDF_BYTES
    assert response.content.iterations == 1
    assert [call[0] for call in calls].count("GET") == 1
    assert remaining == []
    assert resolver.closed == 1


def test_remote_fetch_cleanup_failure_is_redacted_after_all_owners_close(monkeypatch):
    calls = []
    connector_closed = []
    response = _ScriptedResponse(
        headers={
            "Content-Type": "application/pdf",
            "Content-Length": str(len(PDF_BYTES)),
        },
        chunks=[PDF_BYTES],
    )
    _install_scripted_session(monkeypatch, [response], calls)

    class Resolver(_PublicResolver):
        async def close(self):
            self.closed += 1
            raise RuntimeError("resolver leaked secret detail")

    class Connector:
        def __init__(self, **_kwargs):
            pass

        async def close(self):
            connector_closed.append(True)

    monkeypatch.setattr(remote_pdf.aiohttp, "TCPConnector", Connector)
    resolver = Resolver()
    with pytest.raises(RemotePdfImportError) as caught:
        asyncio.run(remote_pdf.fetch_public_pdf(
            "https://public.example/lecture.pdf",
            io.BytesIO(),
            deadline_at=time.monotonic() + 30,
            resolver=resolver,
        ))

    assert caught.value.code == "unavailable"
    assert caught.value.__cause__ is None
    assert "secret" not in str(caught.value)
    assert resolver.closed == 1
    assert connector_closed == [True]


def test_drive_restricted_or_login_html_is_rejected_without_exposing_body_or_id():
    info = _drive_file(_safe_url("https://drive.google.com/open?id=" + "A" * 20))
    with pytest.raises(RemotePdfImportError) as caught: _confirmation_url(b"<html>login secret</html>", info)
    assert "secret" not in str(caught.value) and info.file_id not in str(caught.value)


def test_drive_confirmation_allows_one_exact_google_action_and_same_file_id():
    info = _drive_file(_safe_url("https://drive.google.com/open?id=" + "A" * 20))
    result = _confirmation_url(f'<form method="get" action="https://drive.usercontent.google.com/download"><input name="id" value="{info.file_id}"><input name="export" value="download"><input name="confirm" value="x"></form>'.encode(), info)
    assert "id=" + info.file_id in result and "confirm=x" in result


def test_remote_redirect_revalidates_and_pins_every_hop_and_rejects_private_destination(
    monkeypatch,
):
    calls = []
    resolver = _PublicResolver()
    remaining = _install_scripted_session(
        monkeypatch,
        [_ScriptedResponse(status=302, headers={"Location": "https://127.0.0.1/private"})],
        calls,
    )

    with pytest.raises(RemotePdfImportError) as caught:
        asyncio.run(remote_pdf.fetch_public_pdf(
            "https://public.example/a.pdf",
            io.BytesIO(),
            deadline_at=time.monotonic() + 30,
            resolver=resolver,
        ))

    assert caught.value.code == "private_destination"
    assert [call[0] for call in calls].count("GET") == 1
    assert remaining == []


def test_remote_redirect_and_request_caps_have_no_retry_or_head_probe(monkeypatch):
    calls = []
    resolver = _PublicResolver()
    responses = [
        _ScriptedResponse(status=302, headers={"Location": f"/hop-{index}"})
        for index in range(4)
    ]
    remaining = _install_scripted_session(monkeypatch, responses, calls)

    with pytest.raises(RemotePdfImportError) as caught:
        asyncio.run(remote_pdf.fetch_public_pdf(
            "https://public.example/start",
            io.BytesIO(),
            deadline_at=time.monotonic() + 30,
            resolver=resolver,
        ))

    assert caught.value.code == "not_public"
    methods = [call[0] for call in calls if call[0] != "session"]
    assert methods == ["GET", "GET", "GET", "GET"]
    assert len(remaining) == 0


@pytest.mark.parametrize(
    ("status", "extra_headers"),
    [
        (206, {"Content-Range": "bytes 0-13/14"}),
        (200, {"Content-Range": "bytes 0-13/14"}),
        (201, {}),
        (300, {"Location": "/not-an-approved-redirect"}),
    ],
)
def test_remote_accepts_only_terminal_200_and_rejects_206_content_range_or_unknown_redirect_status(
    monkeypatch, status, extra_headers
):
    calls = []
    headers = {"Content-Type": "application/pdf", **extra_headers}
    remaining = _install_scripted_session(
        monkeypatch,
        [_ScriptedResponse(status=status, headers=headers, chunks=[PDF_BYTES])],
        calls,
    )

    with pytest.raises(RemotePdfImportError):
        asyncio.run(remote_pdf.fetch_public_pdf(
            "https://public.example/a.pdf",
            io.BytesIO(),
            deadline_at=time.monotonic() + 30,
            resolver=_PublicResolver(),
        ))

    assert [call[0] for call in calls].count("GET") == 1
    assert remaining == []


def test_remote_import_boundary_validates_only_a_bounded_header_not_the_full_pdf(
    monkeypatch,
):
    calls = []
    validated = []
    body = b"%PDF-1.7\n" + b"x" * (2 * 1024 * 1024) + b"%%EOF"
    response = _ScriptedResponse(
        headers={"Content-Type": "application/pdf", "Content-Length": str(len(body))},
        chunks=[body[:700_000], body[700_000:1_400_000], body[1_400_000:]],
    )
    _install_scripted_session(monkeypatch, [response], calls)

    def bounded_header_validator(value):
        validated.append(value)
        assert len(value) <= 1029

    monkeypatch.setattr(remote_pdf, "validate_pdf_bytes", bounded_header_validator)
    metadata = asyncio.run(remote_pdf.fetch_public_pdf(
        "https://public.example/a.pdf",
        io.BytesIO(),
        deadline_at=time.monotonic() + 30,
        resolver=_PublicResolver(),
    ))

    assert metadata.size == len(body)
    assert len(validated) == 1
    assert validated[0] == body[:1029]


def test_remote_rejects_declared_oversize_before_reading(monkeypatch):
    calls = []
    response = _ScriptedResponse(
        headers={
            "Content-Type": "application/pdf",
            "Content-Length": str(MAX_INPUT_BYTES + 1),
        },
        chunks=[b"must-not-be-read"],
    )
    _install_scripted_session(monkeypatch, [response], calls)

    with pytest.raises(RemotePdfImportError) as caught:
        asyncio.run(remote_pdf.fetch_public_pdf(
            "https://public.example/a.pdf",
            io.BytesIO(),
            deadline_at=time.monotonic() + 30,
            resolver=_PublicResolver(),
        ))

    assert caught.value.code == "too_large"
    assert response.content.iterations == 0


def test_remote_rejects_streamed_oversize_at_the_first_excess_byte(monkeypatch):
    calls = []
    response = _ScriptedResponse(
        headers={"Content-Type": "application/pdf"},
        chunks=[b"12345", b"6", b"must-not-be-read"],
    )
    _install_scripted_session(monkeypatch, [response], calls)
    monkeypatch.setattr(remote_pdf, "MAX_INPUT_BYTES", 5)
    sink = io.BytesIO()

    with pytest.raises(RemotePdfImportError) as caught:
        asyncio.run(remote_pdf.fetch_public_pdf(
            "https://public.example/a.pdf",
            sink,
            deadline_at=time.monotonic() + 30,
            max_bytes=5,
            resolver=_PublicResolver(),
        ))

    assert caught.value.code == "too_large"
    assert sink.getvalue() == b"12345"


@pytest.mark.parametrize(
    ("headers", "body", "expected_code"),
    [
        (
            {"Content-Type": "application/pdf", "Content-Encoding": "gzip"},
            PDF_BYTES,
            "not_pdf",
        ),
        ({"Content-Type": "text/html"}, b"<html>login</html>", "not_pdf"),
        (
            {"Content-Type": "application/pdf"},
            b"not-a-pdf-but-has-an-end%%EOF",
            "not_pdf",
        ),
    ],
)
def test_remote_rejects_compression_html_and_malformed_pdf(
    monkeypatch, headers, body, expected_code
):
    calls = []
    response = _ScriptedResponse(headers=headers, chunks=[body])
    _install_scripted_session(monkeypatch, [response], calls)

    with pytest.raises(RemotePdfImportError) as caught:
        asyncio.run(remote_pdf.fetch_public_pdf(
            "https://public.example/a.pdf",
            io.BytesIO(),
            deadline_at=time.monotonic() + 30,
            resolver=_PublicResolver(),
        ))

    assert caught.value.code == expected_code


def test_remote_errors_never_expose_transport_url_cookie_html_or_pdf_bytes(monkeypatch):
    secret = "https://public.example/a.pdf?token=secret cookie=private %PDF-body"

    class FailingSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def get(self, *_args, **_kwargs):
            raise remote_pdf.aiohttp.ClientConnectionError(secret)

    monkeypatch.setattr(remote_pdf.aiohttp, "ClientSession", FailingSession)

    with pytest.raises(RemotePdfImportError) as caught:
        asyncio.run(remote_pdf.fetch_public_pdf(
            "https://public.example/a.pdf?token=secret",
            io.BytesIO(),
            deadline_at=time.monotonic() + 30,
            resolver=_PublicResolver(),
        ))

    assert caught.value.code == "unavailable"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert all(part not in str(caught.value) for part in ("secret", "cookie", "%PDF"))


def test_remote_async_dns_dependency_is_direct_and_threaded_resolvers_are_absent():
    assert importlib.util.find_spec("notesbot.async_dns") is not None
    root = Path(__file__).resolve().parents[1]
    requirements = (root / "requirements.txt").read_text(encoding="utf-8").splitlines()
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "dnspython>=2.8,<3" in requirements
    assert '"dnspython>=2.8,<3"' in pyproject
    combined = "\n".join(requirements) + "\n" + pyproject
    assert "aiodns" not in combined
    assert "pycares" not in combined
    # aiohttp 3.10 is the declared floor; custom resolvers therefore return
    # its long-standing dictionary contract without constructing newer helper
    # types that may not exist at that floor.
    resolver_sources = (
        (root / "notesbot" / "async_dns.py").read_text(encoding="utf-8")
        + (root / "notesbot" / "remote_pdf.py").read_text(encoding="utf-8")
    )
    assert "aiohttp.abc.ResolveResult(" not in resolver_sources


def test_async_dns_uses_dnspython_udp_and_cancellation_closes_native_transport(
    monkeypatch,
):
    from notesbot import async_dns

    async def exercise():
        import dns.asyncresolver
        import dns.message
        import dns.rdataclass
        import dns.rdatatype
        import dns.rrset

        loop = asyncio.get_running_loop()
        received = asyncio.Event()
        respond = True

        class DnsStub(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                self.transport = transport

            def datagram_received(self, data, address):
                nonlocal respond
                received.set()
                if not respond:
                    return
                query = dns.message.from_wire(data)
                answer = dns.message.make_response(query)
                rdtype = query.question[0].rdtype
                if rdtype == dns.rdatatype.AAAA:
                    answer.answer.append(dns.rrset.from_text(
                        query.question[0].name,
                        30,
                        dns.rdataclass.IN,
                        dns.rdatatype.AAAA,
                        "2001:4860:4860::8888",
                    ))
                elif rdtype == dns.rdatatype.A:
                    answer.answer.append(dns.rrset.from_text(
                        query.question[0].name,
                        30,
                        dns.rdataclass.IN,
                        dns.rdatatype.A,
                        "8.8.8.8",
                    ))
                self.transport.sendto(answer.to_wire(), address)

        stub_transport, _ = await loop.create_datagram_endpoint(
            DnsStub,
            local_addr=("127.0.0.1", 0),
            family=socket.AF_INET,
        )
        port = stub_transport.get_extra_info("sockname")[1]
        native_query_transports = []
        original_create_datagram_endpoint = loop.create_datagram_endpoint

        async def tracked_create_datagram_endpoint(*args, **kwargs):
            transport, protocol = await original_create_datagram_endpoint(
                *args, **kwargs
            )
            native_query_transports.append(transport)
            return transport, protocol

        loop.create_datagram_endpoint = tracked_create_datagram_endpoint
        dns_resolver = dns.asyncresolver.Resolver(configure=False)
        dns_resolver.nameservers = ["127.0.0.1"]
        dns_resolver.port = port
        dns_resolver.search = []
        monkeypatch.setattr(
            async_dns.dns.asyncresolver,
            "Resolver",
            lambda *args, **kwargs: dns_resolver,
        )

        os_calls = []
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *args, **kwargs: os_calls.append((args, kwargs))
            or (_ for _ in ()).throw(AssertionError("OS lookup used")),
        )
        loop.run_in_executor = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("executor used")
        )
        initial_threads = {thread.ident for thread in threading.enumerate()}
        resolver = async_dns.AsyncDnsResolver(time.monotonic() + 2)
        try:
            rows = await resolver.resolve(
                "public.example", 443, socket.AF_UNSPEC
            )
            assert [(row["host"], row["family"]) for row in rows] == [
                ("2001:4860:4860::8888", socket.AF_INET6),
                ("8.8.8.8", socket.AF_INET),
            ]
            assert all(row["port"] == 443 for row in rows)
            assert all(
                row["flags"]
                == socket.AI_NUMERICHOST | socket.AI_NUMERICSERV
                for row in rows
            )

            respond = False
            received.clear()
            pending = asyncio.create_task(
                resolver.resolve("cancel.example", 443, socket.AF_INET)
            )
            await asyncio.wait_for(received.wait(), timeout=1)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            await asyncio.sleep(0)
            assert native_query_transports
            assert all(transport.is_closing() for transport in native_query_transports)
            assert os_calls == []
            assert getattr(loop, "_default_executor", None) is None
            assert {thread.ident for thread in threading.enumerate()} == initial_threads
        finally:
            await resolver.close()
            loop.create_datagram_endpoint = original_create_datagram_endpoint
            stub_transport.close()

    asyncio.run(exercise())


def test_async_dns_forces_windows_registry_configuration_before_resolver_construction(
    monkeypatch,
):
    from notesbot import async_dns
    import dns.win32util

    calls = []

    class Resolver:
        pass

    monkeypatch.setattr(async_dns.sys, "platform", "win32")
    monkeypatch.setattr(
        dns.win32util,
        "set_config_method",
        lambda method: calls.append(("config", method)),
    )
    monkeypatch.setattr(
        async_dns.dns.asyncresolver,
        "Resolver",
        lambda: calls.append(("resolver", None)) or Resolver(),
    )

    resolver = async_dns.AsyncDnsResolver(time.monotonic() + 1)

    assert calls == [
        ("config", dns.win32util.ConfigMethod.Registry),
        ("resolver", None),
    ]
    asyncio.run(resolver.close())


def test_async_dns_constructs_on_non_windows_without_shadowing_dns_module(
    monkeypatch,
):
    from notesbot import async_dns

    calls = []

    class Resolver:
        pass

    monkeypatch.setattr(async_dns.sys, "platform", "linux")
    monkeypatch.setattr(
        async_dns.dns.asyncresolver,
        "Resolver",
        lambda: calls.append("resolver") or Resolver(),
    )

    resolver = async_dns.AsyncDnsResolver(time.monotonic() + 1)

    assert calls == ["resolver"]
    asyncio.run(resolver.close())


def test_async_dns_translates_provider_failures_to_socket_gaierror(monkeypatch):
    from notesbot import async_dns
    import dns.exception

    class Resolver:
        async def resolve_name(self, *_args, **_kwargs):
            raise dns.exception.DNSException("PRIVATE DNS DETAIL")

    monkeypatch.setattr(async_dns.dns.asyncresolver, "Resolver", Resolver)
    resolver = async_dns.AsyncDnsResolver(time.monotonic() + 1)

    async def exercise():
        with pytest.raises(socket.gaierror) as caught:
            await resolver.resolve("public.example", 443, socket.AF_UNSPEC)
        assert "PRIVATE DNS DETAIL" not in str(caught.value)
        assert caught.value.__cause__ is None
        await resolver.close()

    asyncio.run(exercise())


def test_pinned_resolver_rebuilds_only_canonical_numeric_public_rows():
    class Resolver:
        def __init__(self):
            self.closed = 0

        async def resolve(self, host, port=0, family=0):
            return [
                {
                    "hostname": "attacker.invalid",
                    "host": "2001:4860:4860:0:0:0:0:8888",
                    "port": 9,
                    "family": socket.AF_INET6,
                    "proto": 255,
                    "flags": 0,
                    "extra": "ignored",
                },
                {
                    "hostname": host,
                    "host": "8.8.8.8",
                    "port": 10,
                    "family": socket.AF_INET,
                    "proto": 0,
                    "flags": 0,
                },
                {
                    "host": "8.8.8.8",
                    "family": socket.AF_INET,
                },
            ]

        async def close(self):
            self.closed += 1

    base = Resolver()

    async def exercise():
        resolver = _PinnedResolver(base)
        rows = await resolver.resolve("public.example", 443, socket.AF_UNSPEC)
        assert rows == [
            {
                "hostname": "public.example",
                "host": "2001:4860:4860::8888",
                "port": 443,
                "family": socket.AF_INET6,
                "proto": socket.IPPROTO_TCP,
                "flags": socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
            },
            {
                "hostname": "public.example",
                "host": "8.8.8.8",
                "port": 443,
                "family": socket.AF_INET,
                "proto": socket.IPPROTO_TCP,
                "flags": socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
            },
        ]
        await resolver.close()
        await resolver.close()
        assert base.closed == 1

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "row",
    [
        {"host": "8.8.8.8", "family": socket.AF_INET6},
        {"host": "2001:4860:4860::8888", "family": socket.AF_INET},
        {"host": "8.8.8.8", "family": 999},
        {"host": "not-numeric", "family": socket.AF_INET},
        {"host": "127.0.0.1", "family": socket.AF_INET},
        {"family": socket.AF_INET},
    ],
)
def test_pinned_resolver_rejects_malformed_family_mismatch_and_non_public_rows(row):
    class Resolver:
        async def resolve(self, host, port=0, family=0):
            return [row]

        async def close(self):
            return None

    async def exercise():
        resolver = _PinnedResolver(Resolver())
        with pytest.raises(RemotePdfImportError) as caught:
            await resolver.resolve("public.example", 443, socket.AF_UNSPEC)
        assert caught.value.code == "private_destination"
        await resolver.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", [TimeoutError(), RuntimeError("dns detail")])
def test_pinned_resolver_does_not_misclassify_dns_transport_failures_as_private(failure):
    class Resolver:
        async def resolve(self, host, port=0, family=0):
            raise failure

        async def close(self):
            return None

    async def exercise():
        resolver = _PinnedResolver(Resolver())
        with pytest.raises(type(failure)):
            await resolver.resolve("public.example", 443, socket.AF_UNSPEC)
        await resolver.close()

    asyncio.run(exercise())


def test_pure_async_success_orders_authority_fetch_one_put_and_closes_owned_loop_spool(
    monkeypatch,
):
    steps = []
    spool_closed = threading.Event()
    authority = {
        "Authorization": "Bearer exact-async-authority",
        "x-vercel-blob-store-id": "4mlmgDlkyHHkSimU",
    }

    class Store(BlobStore):
        async def prepare_remote_upload_authority_async(self, *, deadline_at):
            await asyncio.sleep(0)
            loop = asyncio.get_running_loop()
            assert getattr(loop, "_default_executor", None) is None
            steps.append("authority")
            return authority

        async def put_stream_async(
            self, pathname, stream, content_length, content_type, **kwargs
        ):
            await asyncio.sleep(0)
            assert steps == ["authority", "fetch"]
            assert pathname == INPUT_PATH
            assert content_length == len(PDF_BYTES)
            assert content_type == "application/pdf"
            assert kwargs["authority_headers"] is authority
            assert stream.read() == PDF_BYTES
            steps.append("put")
            return BlobRef(
                f"https://{self.store_host}/{pathname}", pathname, '"etag"'
            )

    async def fetch(_url, sink, **_kwargs):
        await asyncio.sleep(0)
        assert steps == ["authority"]
        steps.append("fetch")
        sink.write(PDF_BYTES)
        return remote_pdf.RemotePdfMetadata(len(PDF_BYTES), "lecture.pdf")

    original_spool = remote_pdf.tempfile.SpooledTemporaryFile

    def tracked_spool(*args, **kwargs):
        return _TrackedSpool(original_spool(*args, **kwargs), spool_closed)

    created_loops = []
    original_new_event_loop = remote_pdf.asyncio.new_event_loop

    def tracked_loop():
        loop = original_new_event_loop()
        created_loops.append(loop)
        return loop

    monkeypatch.setattr(remote_pdf, "fetch_public_pdf", fetch)
    monkeypatch.setattr(remote_pdf.tempfile, "SpooledTemporaryFile", tracked_spool)
    monkeypatch.setattr(remote_pdf.asyncio, "new_event_loop", tracked_loop)
    initial_threads = {thread.ident for thread in threading.enumerate()}
    store = Store(STORE_ID)

    imported = remote_pdf.acquire_public_pdf_to_blob_sync(
        "https://public.example/a.pdf",
        store,
        INPUT_PATH,
        max_bytes=MAX_INPUT_BYTES,
    )

    assert imported == remote_pdf.ImportedBlob(
        f"https://{store.store_host}/{INPUT_PATH}",
        len(PDF_BYTES),
        "lecture.pdf",
    )
    assert steps == ["authority", "fetch", "put"]
    assert spool_closed.is_set()
    assert len(created_loops) == 1
    assert created_loops[0].is_closed()
    assert getattr(created_loops[0], "_default_executor", None) is None
    assert not asyncio.all_tasks(created_loops[0])
    assert not getattr(created_loops[0], "_asyncgens", ())
    assert {thread.ident for thread in threading.enumerate()} == initial_threads
    source = Path(remote_pdf.__file__).read_text(encoding="utf-8")
    assert "multiprocessing" not in source
    assert "Process(" not in source


def test_async_caller_cancellation_closes_fetch_and_spool_before_propagating(
    monkeypatch,
):
    entered = asyncio.Event()
    finalized = asyncio.Event()
    spool_closed = threading.Event()

    class Store(BlobStore):
        async def prepare_remote_upload_authority_async(self, *, deadline_at):
            await asyncio.sleep(0)
            return {"Authorization": "Bearer async-authority"}

        async def put_stream_async(self, *_args, **_kwargs):
            raise AssertionError("PUT must not be reached")

    async def blocked_fetch(_url, _sink, **_kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalized.set()

    original_spool = remote_pdf.tempfile.SpooledTemporaryFile

    def tracked_spool(*args, **kwargs):
        return _TrackedSpool(original_spool(*args, **kwargs), spool_closed)

    monkeypatch.setattr(remote_pdf, "fetch_public_pdf", blocked_fetch)
    monkeypatch.setattr(remote_pdf.tempfile, "SpooledTemporaryFile", tracked_spool)
    monkeypatch.setattr(remote_pdf, "REMOTE_BLOB_UPLOAD_RESERVE_SECONDS", 0.05)
    monkeypatch.setattr(remote_pdf, "REMOTE_ACQUISITION_TOTAL_SECONDS", 5.0)

    async def exercise():
        task = asyncio.create_task(remote_pdf.acquire_public_pdf_to_blob(
            "https://public.example/a.pdf",
            Store(STORE_ID),
            INPUT_PATH,
            max_bytes=MAX_INPUT_BYTES,
        ))
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finalized.is_set()
        assert spool_closed.is_set()
        assert not [
            other for other in asyncio.all_tasks()
            if other is not asyncio.current_task()
        ]

    asyncio.run(exercise())


def test_sync_adapter_fails_closed_before_any_executor_thread_can_start(monkeypatch):
    executed = threading.Event()
    created_loops = []
    original_new_event_loop = remote_pdf.asyncio.new_event_loop

    class Store(BlobStore):
        async def prepare_remote_upload_authority_async(self, *, deadline_at):
            await asyncio.to_thread(executed.set)
            raise AssertionError("executor attempt unexpectedly completed")

    def tracked_loop():
        loop = original_new_event_loop()
        created_loops.append(loop)
        return loop

    monkeypatch.setattr(remote_pdf.asyncio, "new_event_loop", tracked_loop)
    initial_threads = {thread.ident for thread in threading.enumerate()}
    with pytest.raises(RemotePdfImportError) as caught:
        remote_pdf.acquire_public_pdf_to_blob_sync(
            "https://public.example/a.pdf",
            Store(STORE_ID),
            INPUT_PATH,
            max_bytes=MAX_INPUT_BYTES,
        )

    assert caught.value.code == "unavailable"
    assert not executed.is_set()
    assert len(created_loops) == 1 and created_loops[0].is_closed()
    assert getattr(created_loops[0], "_default_executor", None) is None
    assert not asyncio.all_tasks(created_loops[0])
    assert {thread.ident for thread in threading.enumerate()} == initial_threads


def test_sync_adapter_deadline_cancels_official_async_oidc_without_survivors(
    monkeypatch,
):
    entered = threading.Event()
    finalized = threading.Event()

    async def blocked_provider():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalized.set()

    monkeypatch.setattr(blob_store, "_get_oidc_token_async", blocked_provider)
    monkeypatch.setattr(remote_pdf, "REMOTE_ACQUISITION_TOTAL_SECONDS", 0.1)
    initial_threads = {thread.ident for thread in threading.enumerate()}
    started = time.monotonic()
    with pytest.raises(RemotePdfImportError) as caught:
        remote_pdf.acquire_public_pdf_to_blob_sync(
            "https://public.example/a.pdf",
            BlobStore(STORE_ID),
            INPUT_PATH,
            max_bytes=MAX_INPUT_BYTES,
        )
    elapsed = time.monotonic() - started

    assert caught.value.code == "deadline_exceeded"
    assert entered.is_set()
    assert finalized.is_set()
    assert elapsed < 0.5
    assert {thread.ident for thread in threading.enumerate()} == initial_threads


@pytest.mark.parametrize(
    "phase",
    ["dns", "connect", "headers", "body", "session_exit", "resolver_close"],
)
def test_sync_adapter_deadline_cancels_real_fetch_and_cleanup_phases_without_survivors(
    monkeypatch, tls_material, phase
):
    entered = threading.Event()
    finalized = threading.Event()
    created_loops = []
    resolver_closed = []
    initial_threads = {thread.ident for thread in threading.enumerate()}
    original_new_event_loop = remote_pdf.asyncio.new_event_loop
    original_start_connection = aiohappyeyeballs.start_connection
    server = None

    async def stall():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalized.set()

    class Resolver:
        async def resolve(self, host, port=0, family=0):
            if phase == "dns":
                await stall()
            return [{
                "hostname": host,
                "host": "8.8.8.8",
                "port": port,
                "family": socket.AF_INET,
                "proto": socket.IPPROTO_TCP,
                "flags": socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
            }]

        async def close(self):
            resolver_closed.append(True)
            if phase == "resolver_close":
                await stall()

    resolver = Resolver()
    monkeypatch.setattr(
        remote_pdf,
        "AsyncDnsResolver",
        lambda *_args, **_kwargs: resolver,
    )
    monkeypatch.setattr(remote_pdf, "REMOTE_ACQUISITION_TOTAL_SECONDS", 0.3)
    monkeypatch.setattr(remote_pdf, "REMOTE_BLOB_UPLOAD_RESERVE_SECONDS", 0.05)

    class Store(BlobStore):
        async def prepare_remote_upload_authority_async(self, *, deadline_at):
            await asyncio.sleep(0)
            return {"Authorization": "Bearer phase-authority"}

        async def put_stream_async(self, *_args, **_kwargs):
            raise AssertionError("PUT must not be reached")

    if phase == "connect":
        async def blocked_connection(**_kwargs):
            await stall()

        monkeypatch.setattr(
            aiohappyeyeballs,
            "start_connection",
            blocked_connection,
        )
    elif phase in {"headers", "body"}:
        cert_path, key_path = tls_material
        server = _TlsScenarioServer(phase, cert_path, key_path, entered)

        async def controlled_connection(*, addr_infos, **kwargs):
            redirected = [
                (family, kind, proto, canonname, ("127.0.0.1", server.port))
                for family, kind, proto, canonname, _address in addr_infos
            ]
            return await original_start_connection(addr_infos=redirected, **kwargs)

        monkeypatch.setattr(
            aiohappyeyeballs,
            "start_connection",
            controlled_connection,
        )
        import aiohttp.connector as connector_module
        trusted_context = ssl.create_default_context(cafile=str(cert_path))
        trusted_context.set_alpn_protocols(("http/1.1",))
        monkeypatch.setattr(
            connector_module,
            "_SSL_CONTEXT_VERIFIED",
            trusted_context,
        )
    elif phase in {"session_exit", "resolver_close"}:
        response = _ScriptedResponse(
            headers={
                "Content-Type": "application/pdf",
                "Content-Length": str(len(PDF_BYTES)),
            },
            chunks=[PDF_BYTES],
        )

        class Request:
            async def __aenter__(self):
                return response

            async def __aexit__(self, *_args):
                return None

        class Session:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                if phase == "session_exit":
                    await stall()

            def get(self, *_args, **_kwargs):
                return Request()

        monkeypatch.setattr(remote_pdf.aiohttp, "ClientSession", Session)

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("OS resolver used")
        ),
    )

    def tracked_loop():
        loop = original_new_event_loop()
        created_loops.append(loop)
        return loop

    monkeypatch.setattr(remote_pdf.asyncio, "new_event_loop", tracked_loop)
    started = time.monotonic()
    try:
        with pytest.raises(RemotePdfImportError) as caught:
            remote_pdf.acquire_public_pdf_to_blob_sync(
                "https://public.example/a.pdf",
                Store(STORE_ID),
                INPUT_PATH,
                max_bytes=MAX_INPUT_BYTES,
            )
        elapsed = time.monotonic() - started
        assert caught.value.code == "deadline_exceeded"
        assert entered.is_set()
        if phase not in {"headers", "body"}:
            assert finalized.is_set()
        assert elapsed < 0.8
        assert resolver_closed == [True]
        assert len(created_loops) == 1 and created_loops[0].is_closed()
        assert not asyncio.all_tasks(created_loops[0])
        assert getattr(created_loops[0], "_default_executor", None) is None
        if server is not None:
            assert server.peer_closed.wait(0.5)
    finally:
        if server is not None:
            server.close()
    assert {thread.ident for thread in threading.enumerate()} == initial_threads


def test_sync_adapter_deadline_cancels_actual_private_blob_put_and_closes_spool(
    monkeypatch, tls_material
):
    entered = threading.Event()
    spool_closed = threading.Event()
    resolver_closed = threading.Event()
    created_loops = []
    destinations = []
    original_loop = remote_pdf.asyncio.new_event_loop
    original_start_connection = aiohappyeyeballs.start_connection
    cert_path, key_path = tls_material
    initial_threads = {thread.ident for thread in threading.enumerate()}
    server = _TlsScenarioServer("blob_response", cert_path, key_path, entered)

    class Store(BlobStore):
        async def prepare_remote_upload_authority_async(self, *, deadline_at):
            await asyncio.sleep(0)
            return {
                "Authorization": "Bearer exact-authority",
                "x-vercel-blob-store-id": self.store_key,
                "x-api-blob-request-id": "async:test",
                "x-api-blob-request-attempt": "0",
                "x-api-version": "12",
            }

    class Resolver:
        async def resolve(self, host, port=0, family=0):
            return [{
                "hostname": host,
                "host": "8.8.8.8",
                "port": port,
                "family": socket.AF_INET,
                "proto": socket.IPPROTO_TCP,
                "flags": socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
            }]

        async def close(self):
            resolver_closed.set()

    async def fetch(_url, sink, **_kwargs):
        await asyncio.sleep(0)
        sink.write(PDF_BYTES)
        return remote_pdf.RemotePdfMetadata(len(PDF_BYTES), "lecture.pdf")

    original_spool = remote_pdf.tempfile.SpooledTemporaryFile

    def tracked_spool(*args, **kwargs):
        return _TrackedSpool(original_spool(*args, **kwargs), spool_closed)

    def tracked_loop():
        loop = original_loop()
        created_loops.append(loop)
        return loop

    async def controlled_connection(*, addr_infos, **kwargs):
        destinations.extend(address[-1] for address in addr_infos)
        redirected = [
            (family, kind, proto, canonname, ("127.0.0.1", server.port))
            for family, kind, proto, canonname, _address in addr_infos
        ]
        return await original_start_connection(addr_infos=redirected, **kwargs)

    monkeypatch.setattr(remote_pdf, "fetch_public_pdf", fetch)
    monkeypatch.setattr(remote_pdf.tempfile, "SpooledTemporaryFile", tracked_spool)
    monkeypatch.setattr(remote_pdf, "REMOTE_ACQUISITION_TOTAL_SECONDS", 0.4)
    monkeypatch.setattr(blob_store, "AsyncDnsResolver", lambda *_args, **_kwargs: Resolver())
    monkeypatch.setattr(aiohappyeyeballs, "start_connection", controlled_connection)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("OS resolver used")
        ),
    )
    import aiohttp.connector as connector_module
    trusted_context = ssl.create_default_context(cafile=str(cert_path))
    trusted_context.set_alpn_protocols(("http/1.1",))
    monkeypatch.setattr(connector_module, "_SSL_CONTEXT_VERIFIED", trusted_context)
    monkeypatch.setattr(remote_pdf.asyncio, "new_event_loop", tracked_loop)
    started = time.monotonic()
    try:
        with pytest.raises(RemotePdfImportError) as caught:
            remote_pdf.acquire_public_pdf_to_blob_sync(
                "https://public.example/a.pdf",
                Store(STORE_ID),
                INPUT_PATH,
                max_bytes=MAX_INPUT_BYTES,
            )
        assert server.peer_closed.wait(0.5)
    finally:
        server.close()

    assert caught.value.code == "deadline_exceeded"
    assert time.monotonic() - started < 0.9
    assert entered.is_set()
    assert destinations == [("8.8.8.8", 443)]
    assert resolver_closed.is_set()
    assert spool_closed.is_set()
    assert len(created_loops) == 1 and created_loops[0].is_closed()
    assert not asyncio.all_tasks(created_loops[0])
    assert getattr(created_loops[0], "_default_executor", None) is None
    assert {thread.ident for thread in threading.enumerate()} == initial_threads
