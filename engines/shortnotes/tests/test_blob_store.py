"""Boundary tests for the Blob REST adapter (all HTTP is faked)."""
from __future__ import annotations

import json
import asyncio
import io
import time
import threading

import pytest

from notesbot import blob_store
from notesbot.blob_store import (
    BlobConflictError,
    BlobNotFoundError,
    BlobStore,
    validate_blob_url,
    validate_pdf_bytes,
)


STORE_KEY = "4mlmgDlkyHHkSimU"
STORE_ID = f"store_{STORE_KEY}"
HOST = f"{STORE_KEY.lower()}.private.blob.vercel-storage.com"
APP_PREFIX = "shortnotes/v1/"
INPUT_PATH = f"{APP_PREFIX}inputs/{'b' * 32}/{'a' * 32}.pdf"
JOB_PATH = f"{APP_PREFIX}jobs/{'b' * 32}/{'a' * 32}.json"
INPUT = f"https://{HOST}/{INPUT_PATH}"
JOB = f"https://{HOST}/{JOB_PATH}"
ETAG_1 = '"etag-v1"'
ETAG_2 = '"etag-v2"'


class Response:
    def __init__(
        self,
        *,
        status=200,
        headers=None,
        chunks=(),
        value=None,
        close_error=None,
    ):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = list(chunks)
        self._value = value
        self._close_error = close_error
        self.close_count = 0

    def iter_content(self, chunk_size):
        yield from self._chunks

    def json(self):
        return self._value

    def close(self):
        self.close_count += 1
        if self._close_error is not None:
            raise self._close_error


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


class RaisingSession:
    def request(self, method, url, **kwargs):
        raise RuntimeError("request leaked oidc=super-secret and provider details")


class InvalidJsonResponse(Response):
    def json(self):
        raise ValueError("response leaked oidc=super-secret and provider details")


class RaisingStreamResponse(Response):
    def iter_content(self, chunk_size):
        raise ValueError("stream leaked oidc=super-secret and provider details")


@pytest.fixture(autouse=True)
def oidc_token(monkeypatch):
    monkeypatch.setattr(blob_store, "_get_oidc_token", lambda: "short-lived-oidc", raising=False)


def test_remote_authority_uses_cancellable_official_async_seam(monkeypatch):
    entered = asyncio.Event()
    finalized = asyncio.Event()

    async def blocked_provider():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalized.set()

    monkeypatch.setattr(blob_store, "_get_oidc_token_async", blocked_provider)
    store = BlobStore(STORE_ID, session=Session(Response()))

    async def exercise():
        with pytest.raises(TimeoutError, match="deadline"):
            await store.prepare_remote_upload_authority_async(
                deadline_at=time.monotonic() + 0.02
            )
        assert entered.is_set()
        assert finalized.is_set()
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

    asyncio.run(exercise())


def test_remote_blob_put_uses_explicit_no_executor_resolver_and_closes_every_owner(
    monkeypatch,
):
    events = []
    connectors = []

    class Resolver:
        def __init__(self):
            self.closed = 0

        async def resolve(self, host, port=0, family=0):
            raise AssertionError("the fake HTTP session must not resolve")

        async def close(self):
            self.closed += 1
            events.append("resolver-close")

    class Connector:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = 0
            connectors.append(self)
            events.append("connector-open")

        async def close(self):
            self.closed += 1
            events.append("connector-close")

    class Content:
        async def read(self, _limit):
            return json.dumps({
                "url": INPUT,
                "pathname": INPUT_PATH,
                "contentType": "application/pdf",
                "etag": ETAG_1,
            }).encode()

    class AsyncResponse:
        status = 200
        headers = {"Content-Type": "application/json"}
        content = Content()

    class Request:
        def __init__(self, data):
            self.data = data

        async def __aenter__(self):
            body = bytearray()
            async for chunk in self.data:
                body.extend(chunk)
            assert bytes(body) == b"%PDF-1.7\n%%EOF"
            events.append("response-open")
            return AsyncResponse()

        async def __aexit__(self, *_args):
            events.append("response-close")

    class AsyncSession:
        def __init__(self, *, connector, **kwargs):
            self.connector = connector
            assert kwargs["trust_env"] is False
            assert kwargs["connector_owner"] is False
            events.append("session-open")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            events.append("session-close")

        def put(self, _url, **kwargs):
            events.append("put")
            return Request(kwargs["data"])

    monkeypatch.setattr(blob_store.aiohttp, "TCPConnector", Connector)
    monkeypatch.setattr(blob_store.aiohttp, "ClientSession", AsyncSession)
    resolver = Resolver()
    initial_threads = {thread.ident for thread in threading.enumerate()}
    store = BlobStore(STORE_ID, session=Session(Response()))

    async def exercise():
        loop = asyncio.get_running_loop()
        loop.run_in_executor = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("executor used")
        )
        ref = await store.put_stream_async(
            INPUT_PATH,
            io.BytesIO(b"%PDF-1.7\n%%EOF"),
            14,
            "application/pdf",
            max_bytes=100,
            deadline_at=time.monotonic() + 2,
            authority_headers=store.prepare_remote_upload_authority(),
            resolver=resolver,
        )
        assert ref.url == INPUT
        assert getattr(loop, "_default_executor", None) is None
        assert not [
            task for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
        ]

    asyncio.run(exercise())
    assert resolver.closed == 1
    assert connectors[0].kwargs["resolver"] is resolver
    assert connectors[0].kwargs["use_dns_cache"] is False
    assert connectors[0].kwargs["force_close"] is True
    assert events == [
        "connector-open",
        "session-open",
        "put",
        "response-open",
        "response-close",
        "session-close",
        "connector-close",
        "resolver-close",
    ]
    assert {thread.ident for thread in threading.enumerate()} == initial_threads


def test_remote_blob_cleanup_failure_is_redacted_after_all_owners_close(monkeypatch):
    closed = []

    class Resolver:
        async def resolve(self, host, port=0, family=0):
            return []

        async def close(self):
            closed.append("resolver")

    class Connector:
        def __init__(self, **_kwargs):
            pass

        async def close(self):
            closed.append("connector")

    class Content:
        async def read(self, _limit):
            return json.dumps({
                "url": INPUT,
                "pathname": INPUT_PATH,
                "contentType": "application/pdf",
                "etag": ETAG_1,
            }).encode()

    class AsyncResponse:
        status = 200
        headers = {"Content-Type": "application/json"}
        content = Content()

    class Request:
        async def __aenter__(self):
            return AsyncResponse()

        async def __aexit__(self, *_args):
            return None

    class Session:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            closed.append("session")

        def put(self, *_args, **_kwargs):
            return Request()

    async def failing_iterator_close(self):
        closed.append("iterator")
        raise RuntimeError("iterator leaked secret detail")

    monkeypatch.setattr(blob_store.aiohttp, "TCPConnector", Connector)
    monkeypatch.setattr(blob_store.aiohttp, "ClientSession", Session)
    monkeypatch.setattr(
        blob_store._StreamChunkIterator,
        "aclose",
        failing_iterator_close,
    )
    store = BlobStore(STORE_ID, session=Session())
    with pytest.raises(RuntimeError, match="unavailable") as caught:
        asyncio.run(store.put_stream_async(
            INPUT_PATH,
            io.BytesIO(b"%PDF-1.7\n%%EOF"),
            14,
            "application/pdf",
            max_bytes=100,
            deadline_at=time.monotonic() + 1,
            authority_headers=store.prepare_remote_upload_authority(),
            resolver=Resolver(),
        ))

    assert caught.value.__cause__ is None
    assert "secret" not in str(caught.value)
    assert sorted(closed) == ["connector", "iterator", "resolver", "session"]


@pytest.mark.parametrize("phase", ["response", "session_exit", "resolver_close", "iterator_close"])
def test_remote_blob_deadline_cancels_actual_async_cleanup_phases(monkeypatch, phase):
    entered = asyncio.Event()
    finalized = asyncio.Event()
    owners = {"session": 0, "connector": 0, "resolver": 0, "iterator": 0}
    initial_threads = {thread.ident for thread in threading.enumerate()}

    async def stall():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalized.set()

    class Resolver:
        async def resolve(self, host, port=0, family=0):
            return []

        async def close(self):
            owners["resolver"] += 1
            if phase == "resolver_close":
                await stall()

    class Connector:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def close(self):
            owners["connector"] += 1

    class Content:
        async def read(self, _limit):
            return json.dumps({
                "url": INPUT,
                "pathname": INPUT_PATH,
                "contentType": "application/pdf",
                "etag": ETAG_1,
            }).encode()

    class AsyncResponse:
        status = 200
        headers = {"Content-Type": "application/json"}
        content = Content()

    class Request:
        async def __aenter__(self):
            if phase == "response":
                await stall()
            return AsyncResponse()

        async def __aexit__(self, *_args):
            return None

    class AsyncSession:
        def __init__(self, *, connector, **_kwargs):
            self.connector = connector

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            owners["session"] += 1
            if phase == "session_exit":
                await stall()

        def put(self, _url, **_kwargs):
            return Request()

    monkeypatch.setattr(blob_store.aiohttp, "TCPConnector", Connector)
    monkeypatch.setattr(blob_store.aiohttp, "ClientSession", AsyncSession)
    original_iterator = getattr(blob_store, "_StreamChunkIterator", None)

    if original_iterator is not None and phase == "iterator_close":
        original_close = original_iterator.aclose

        async def blocked_close(self):
            owners["iterator"] += 1
            await stall()

        monkeypatch.setattr(original_iterator, "aclose", blocked_close)

    store = BlobStore(STORE_ID, session=Session(Response()))

    async def exercise():
        loop = asyncio.get_running_loop()
        loop.run_in_executor = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("executor used")
        )
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await store.put_stream_async(
                INPUT_PATH,
                io.BytesIO(b"%PDF-1.7\n%%EOF"),
                14,
                "application/pdf",
                max_bytes=100,
                deadline_at=time.monotonic() + 0.2,
                authority_headers=store.prepare_remote_upload_authority(),
                resolver=Resolver(),
            )
        assert time.monotonic() - started < 0.6
        assert entered.is_set()
        assert finalized.is_set()
        assert owners["resolver"] == 1
        assert owners["connector"] == 1
        assert getattr(loop, "_default_executor", None) is None
        assert not [
            task for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
        ]

    if phase == "iterator_close" and original_iterator is None:
        with pytest.raises(AssertionError, match="iterator implementation missing"):
            raise AssertionError("iterator implementation missing")
    else:
        asyncio.run(exercise())
        assert {thread.ident for thread in threading.enumerate()} == initial_threads


def test_blob_url_accepts_only_normalized_https_path_on_exact_private_host():
    assert validate_blob_url(INPUT, HOST, f"{APP_PREFIX}inputs/") == INPUT


def test_import_stream_shorter_or_longer_than_declared_fails_before_put_without_retry(monkeypatch):
    store = BlobStore(STORE_ID, session=Session(Response()))
    opened = []
    class ForbiddenSession:
        def __init__(self, *args, **kwargs): opened.append(True)
    monkeypatch.setattr(blob_store.aiohttp, "ClientSession", ForbiddenSession)
    for stream, length in ((io.BytesIO(b"short"), 6), (io.BytesIO(b"longer"), 5)):
        with pytest.raises(ValueError, match="Invalid Blob upload"):
            asyncio.run(store.put_stream_async(INPUT_PATH, stream, length, "application/pdf", max_bytes=100, deadline_at=10**12))
    assert opened == []


def test_remote_blob_put_is_one_create_only_private_request_with_the_prefetched_authority(monkeypatch):
    store = BlobStore(STORE_ID, session=Session(Response()))
    captured = []
    class Content:
        async def read(self, _limit):
            return json.dumps({"url": INPUT, "pathname": INPUT_PATH, "contentType": "application/pdf", "etag": ETAG_1}).encode()
    class AsyncResponse:
        status = 200
        headers = {"Content-Type": "application/json"}
        content = Content()
    class Request:
        async def __aenter__(self): return AsyncResponse()
        async def __aexit__(self, *_args): pass
    class AsyncSession:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        def put(self, url, **kwargs): captured.append((url, kwargs)); return Request()
    monkeypatch.setattr(blob_store.aiohttp, "ClientSession", AsyncSession)
    authority = store.prepare_remote_upload_authority()
    ref = asyncio.run(store.put_stream_async(INPUT_PATH, io.BytesIO(b"%PDF-1.7\n%%EOF"), 14, "application/pdf", max_bytes=100, deadline_at=time.monotonic() + 2, authority_headers=authority))
    headers = captured[0][1]["headers"]
    assert ref.url == INPUT and len(captured) == 1
    assert headers["Authorization"] == authority["Authorization"]
    assert headers["x-vercel-blob-access"] == "private"
    assert headers["x-add-random-suffix"] == "0" and headers["x-allow-overwrite"] == "0"


def test_ambiguous_post_put_transport_error_never_retries_or_overwrites(monkeypatch):
    store = BlobStore(STORE_ID, session=Session(Response())); attempts = []
    class Request:
        async def __aenter__(self):
            attempts.append("possibly-committed")
            raise blob_store.aiohttp.ClientConnectionError("lost response")
        async def __aexit__(self, *_args): pass
    class AsyncSession:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        def put(self, _url, **kwargs):
            assert kwargs["headers"]["x-allow-overwrite"] == "0"
            return Request()
    monkeypatch.setattr(blob_store.aiohttp, "ClientSession", AsyncSession)
    with pytest.raises(RuntimeError, match="unavailable"):
        asyncio.run(store.put_stream_async(INPUT_PATH, io.BytesIO(b"%PDF-1.7\n%%EOF"), 14, "application/pdf", max_bytes=100, deadline_at=time.monotonic() + 2, authority_headers=store.prepare_remote_upload_authority()))
    assert attempts == ["possibly-committed"]


@pytest.mark.parametrize(
    "url",
    [
        f"http://{HOST}/{INPUT_PATH}",
        "https://169.254.169.254/latest",
        f"https://[::1]/{INPUT_PATH}",
        f"https://user:pass@{HOST}/{INPUT_PATH}",
        f"https://{HOST}.evil.example/{INPUT_PATH}",
        f"https://evil-{HOST}/{INPUT_PATH}",
        f"https://{HOST}/{APP_PREFIX}outputs/a.pdf",
        f"https://{HOST}/{APP_PREFIX}inputs/%2e%2e/outputs/a.pdf",
        f"https://{HOST}/{APP_PREFIX}inputs/%252e%252e/outputs/a.pdf",
        f"https://{HOST}/{INPUT_PATH}#fragment",
        f"https://{HOST}:443/{INPUT_PATH}",
    ],
)
def test_blob_url_rejects_ssrf_and_path_confusion(url):
    with pytest.raises(ValueError):
        validate_blob_url(url, HOST, f"{APP_PREFIX}inputs/")


def test_get_bytes_disables_redirects_and_streams_under_cap():
    session = Session(Response(headers={"Content-Length": "5"}, chunks=[b"ab", b"cde"]))
    store = BlobStore(STORE_ID, session=session)

    assert store.get_bytes(INPUT, max_bytes=5) == b"abcde"
    method, url, kwargs = session.calls[0]
    assert (method, url) == ("GET", INPUT)
    assert kwargs["allow_redirects"] is False
    assert kwargs["stream"] is True
    assert kwargs["headers"]["Authorization"] == "Bearer short-lived-oidc"
    assert kwargs["headers"]["x-vercel-blob-store-id"] == STORE_KEY
    assert kwargs["headers"]["x-api-version"] == "12"
    assert kwargs["headers"]["x-api-blob-request-attempt"] == "0"
    assert kwargs["headers"]["x-api-blob-request-id"].startswith(f"{STORE_KEY}:")
    assert session.response.close_count == 1


def test_get_bytes_rejects_redirect_and_content_length_before_buffering():
    redirect = Session(Response(status=302, headers={"Location": "https://evil.example/a"}))
    store = BlobStore(STORE_ID, session=redirect)
    with pytest.raises(RuntimeError, match="operation failed"):
        store.get_bytes(INPUT, max_bytes=10)
    assert redirect.response.close_count == 1

    oversized = Session(Response(headers={"Content-Length": "11"}, chunks=[b"never-read"]))
    store = BlobStore(STORE_ID, session=oversized)
    with pytest.raises(ValueError, match="too large"):
        store.get_bytes(INPUT, max_bytes=10)
    assert oversized.response._chunks == [b"never-read"]
    assert oversized.response.close_count == 1


def test_get_bytes_rejects_actual_stream_oversize_without_exceeding_cap():
    session = Session(Response(chunks=[b"12345", b"6"]))
    store = BlobStore(STORE_ID, session=session)
    with pytest.raises(ValueError, match="too large"):
        store.get_bytes(INPUT, max_bytes=5)
    assert session.response.close_count == 1


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (404, BlobNotFoundError),
        (409, BlobConflictError),
        (412, BlobConflictError),
        (429, RuntimeError),
        (503, RuntimeError),
    ],
)
def test_get_bytes_closes_status_error_responses_exactly_once(status, error_type):
    session = Session(Response(status=status, chunks=[b"never-read"]))

    with pytest.raises(error_type):
        BlobStore(STORE_ID, session=session).get_bytes(INPUT, max_bytes=10)

    assert session.response._chunks == [b"never-read"]
    assert session.response.close_count == 1


@pytest.mark.parametrize("declared", ["invalid", "-1", "11"])
def test_get_bytes_closes_invalid_length_responses_exactly_once(declared):
    session = Session(
        Response(headers={"Content-Length": declared}, chunks=[b"never-read"])
    )

    with pytest.raises((RuntimeError, ValueError)):
        BlobStore(STORE_ID, session=session).get_bytes(INPUT, max_bytes=10)

    assert session.response._chunks == [b"never-read"]
    assert session.response.close_count == 1


def test_get_bytes_closes_iterator_failure_without_masking_the_fixed_error():
    response = RaisingStreamResponse()
    store = BlobStore(STORE_ID, session=Session(response))

    with pytest.raises(RuntimeError, match="unavailable") as caught:
        store.get_bytes(INPUT, max_bytes=10)

    assert caught.value.__cause__ is None
    assert response.close_count == 1


def test_response_close_failure_never_replaces_success_or_primary_error():
    success = Response(chunks=[b"ok"], close_error=RuntimeError("close details"))
    assert BlobStore(STORE_ID, session=Session(success)).get_bytes(INPUT, 10) == b"ok"
    assert success.close_count == 1

    failure = Response(
        headers={"Content-Length": "11"},
        chunks=[b"never-read"],
        close_error=RuntimeError("close details"),
    )
    with pytest.raises(ValueError, match="too large") as caught:
        BlobStore(STORE_ID, session=Session(failure)).get_bytes(INPUT, 10)
    assert caught.value.__cause__ is None
    assert failure.close_count == 1


@pytest.mark.parametrize("data", [b"%PDF-1.7\n", b" \r\n\t%PDF-1.7\n"])
def test_pdf_header_accepts_only_tolerable_leading_whitespace(data):
    validate_pdf_bytes(data)


@pytest.mark.parametrize("data", [b"not a pdf", b"junk%PDF-1.7", b" " * 1025 + b"%PDF-1.7"])
def test_pdf_header_rejects_junk_or_distant_header(data):
    with pytest.raises(ValueError, match="PDF"):
        validate_pdf_bytes(data)


def test_get_pdf_accepts_pdf_mime_with_normal_parameter():
    session = Session(
        Response(
            headers={"Content-Type": "application/pdf; charset=binary"},
            chunks=[b"%PDF-1.7\n"],
        )
    )
    assert BlobStore(STORE_ID, session=session).get_pdf(INPUT, 100) == b"%PDF-1.7\n"


@pytest.mark.parametrize("content_type", [None, "text/html", "application/pdf; broken"])
def test_get_pdf_rejects_missing_non_pdf_or_malformed_mime(content_type):
    headers = {} if content_type is None else {"Content-Type": content_type}
    session = Session(Response(headers=headers, chunks=[b"%PDF-1.7\n"]))
    with pytest.raises(ValueError, match="PDF content type"):
        BlobStore(STORE_ID, session=session).get_pdf(INPUT, 100)


def test_put_json_and_delete_use_official_private_oidc_protocol():
    response = Response(
        headers={"Content-Type": "application/json"},
        value={
            "url": JOB,
            "pathname": JOB_PATH,
            "contentType": "application/json",
            "etag": ETAG_1,
        },
    )
    session = Session(response)
    store = BlobStore(STORE_ID, session=session)

    ref = store.put_json(JOB_PATH, {"ok": True})
    assert (ref.url, ref.etag) == (JOB, ETAG_1)
    method, url, kwargs = session.calls[0]
    assert (method, url) == (
        "PUT",
        f"https://vercel.com/api/blob/?pathname={JOB_PATH.replace('/', '%2F')}",
    )
    assert json.loads(kwargs["data"].decode()) == {"ok": True}
    assert kwargs["headers"]["Authorization"] == "Bearer short-lived-oidc"
    assert kwargs["headers"]["x-vercel-blob-store-id"] == STORE_KEY
    assert kwargs["headers"]["x-api-version"] == "12"
    assert kwargs["headers"]["x-api-blob-request-attempt"] == "0"
    assert kwargs["headers"]["x-api-blob-request-id"].startswith(f"{STORE_KEY}:")
    assert kwargs["headers"]["x-vercel-blob-access"] == "private"
    assert kwargs["headers"]["x-content-type"] == "application/json"
    assert kwargs["headers"]["x-add-random-suffix"] == "0"
    assert kwargs["headers"]["x-allow-overwrite"] == "0"

    store.delete([JOB])
    method, url, kwargs = session.calls[1]
    assert (method, url) == ("POST", "https://vercel.com/api/blob/delete")
    assert kwargs["headers"]["Authorization"] == "Bearer short-lived-oidc"
    assert kwargs["headers"]["x-vercel-blob-store-id"] == STORE_KEY
    assert kwargs["headers"]["x-api-version"] == "12"
    assert kwargs["headers"]["Content-Type"] == "application/json"
    assert json.loads(kwargs["data"].decode()) == {"urls": [JOB]}


@pytest.mark.parametrize(
    ("headers", "value"),
    [
        (
            {"Content-Type": "text/plain"},
            {"url": INPUT, "pathname": INPUT_PATH, "contentType": "application/pdf", "etag": ETAG_1},
        ),
        (
            {"Content-Type": "application/json"},
            {"url": "https://evil.example/steal", "pathname": INPUT_PATH, "contentType": "application/pdf", "etag": ETAG_1},
        ),
        (
            {"Content-Type": "application/json"},
            {"url": INPUT, "pathname": "../elsewhere", "contentType": "application/pdf", "etag": ETAG_1},
        ),
        (
            {"Content-Type": "application/json"},
            {"url": INPUT, "pathname": INPUT_PATH, "contentType": "text/html", "etag": ETAG_1},
        ),
        (
            {"Content-Type": "application/json"},
            {"url": INPUT, "pathname": INPUT_PATH, "contentType": "application/pdf"},
        ),
    ],
)
def test_put_rejects_untrusted_response_host_path_or_mime(headers, value):
    store = BlobStore(STORE_ID, session=Session(Response(headers=headers, value=value)))
    with pytest.raises(RuntimeError, match="invalid response") as caught:
        store.put_bytes(INPUT_PATH, b"%PDF-1.7", "application/pdf")
    assert caught.value.__cause__ is None


def test_upload_path_rejects_double_encoded_traversal():
    store = BlobStore(STORE_ID, session=Session(Response(value={})))
    with pytest.raises(ValueError):
        store.put_bytes(f"{APP_PREFIX}inputs/%252e%252e/escape.pdf", b"x", "application/pdf")


def test_storage_operations_reject_paths_outside_the_app_namespace():
    session = Session(Response())
    store = BlobStore(STORE_ID, session=session)

    with pytest.raises(ValueError, match="pathname"):
        store.put_bytes("unrelated/a.txt", b"x", "text/plain")
    with pytest.raises(ValueError, match="Blob URL"):
        store.delete([f"https://{HOST}/unrelated/a.txt"])

    assert session.calls == []


@pytest.mark.parametrize(
    ("status", "error_type"),
    [(404, BlobNotFoundError), (409, BlobConflictError), (412, BlobConflictError)],
)
def test_blob_statuses_distinguish_missing_or_conflicting_records_from_outages(status, error_type):
    store = BlobStore(STORE_ID, session=Session(Response(status=status)))
    operation = (
        (lambda: store.get_bytes(JOB, 100))
        if status == 404
        else (lambda: store.put_json(JOB_PATH, {"state": "queued"}))
    )

    with pytest.raises(error_type) as caught:
        operation()
    assert caught.value.__cause__ is None


def test_create_only_put_classifies_the_provider_existing_blob_400_as_a_conflict():
    response = Response(
        status=400,
        headers={"Content-Type": "application/json"},
        value={
            "error": {
                "code": "bad_request",
                "message": (
                    "This blob already exists, use `allowOverwrite: true` if you "
                    "want to overwrite it."
                ),
            }
        },
    )
    store = BlobStore(STORE_ID, session=Session(response))

    with pytest.raises(BlobConflictError) as caught:
        store.put_json(JOB_PATH, {"state": "queued"}, allow_overwrite=False)

    assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    ("allow_overwrite", "code", "message"),
    [
        (False, "bad_request", "Invalid pathname"),
        (False, "not_allowed", "This blob already exists, but access is denied"),
        (True, "bad_request", "This blob already exists, use `allowOverwrite: true`"),
    ],
)
def test_ambiguous_provider_400_is_not_misclassified_as_a_create_collision(
    allow_overwrite, code, message
):
    response = Response(
        status=400,
        headers={"Content-Type": "application/json"},
        value={"error": {"code": code, "message": message}},
    )
    store = BlobStore(STORE_ID, session=Session(response))

    with pytest.raises(RuntimeError, match="operation failed") as caught:
        store.put_json(
            JOB_PATH,
            {"state": "queued"},
            allow_overwrite=allow_overwrite,
        )

    assert type(caught.value) is RuntimeError
    assert caught.value.__cause__ is None


def test_get_json_is_bounded_mime_checked_and_returns_a_strong_etag():
    session = Session(
        Response(
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "ETag": ETAG_1,
                "Content-Length": "18",
            },
            chunks=[b'{"state":"queued"}'],
        )
    )

    document = BlobStore(STORE_ID, session=session).get_json(JOB, max_bytes=100)

    assert document.value == {"state": "queued"}
    assert document.etag == ETAG_1


def test_get_json_bypasses_the_blob_cdn_only_after_validating_the_canonical_url():
    session = Session(
        Response(
            headers={"Content-Type": "application/json", "ETag": ETAG_1},
            chunks=[b'{"state":"queued"}'],
        )
    )

    BlobStore(STORE_ID, session=session).get_json(JOB, max_bytes=100)

    assert session.calls[0][1] == f"{JOB}?cache=0"
    with pytest.raises(ValueError, match="Blob URL"):
        BlobStore(STORE_ID, session=session).get_json(f"{JOB}?cache=attacker", 100)
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    ("headers", "chunks"),
    [
        ({"Content-Type": "text/plain", "ETag": ETAG_1}, [b'{"ok":true}']),
        ({"Content-Type": "application/json", "ETag": ETAG_1}, [b"not-json"]),
        ({"Content-Type": "application/json", "ETag": ETAG_1}, [b"[]"]),
        ({"Content-Type": "application/json"}, [b'{"ok":true}']),
        ({"Content-Type": "application/json", "ETag": "weak-or-malformed"}, [b'{"ok":true}']),
    ],
)
def test_get_json_rejects_unsafe_provider_documents(headers, chunks):
    store = BlobStore(STORE_ID, session=Session(Response(headers=headers, chunks=chunks)))
    with pytest.raises(RuntimeError, match="invalid response") as caught:
        store.get_json(JOB, max_bytes=100)
    assert caught.value.__cause__ is None


def test_conditional_json_update_sends_verified_etag_protocol_and_returns_new_version():
    response = Response(
        headers={"Content-Type": "application/json"},
        value={
            "url": JOB,
            "pathname": JOB_PATH,
            "contentType": "application/json",
            "etag": ETAG_2,
        },
    )
    session = Session(response)
    store = BlobStore(STORE_ID, session=session)

    ref = store.put_json(
        JOB_PATH,
        {"state": "running"},
        allow_overwrite=True,
        if_match=ETAG_1,
    )

    assert ref.etag == ETAG_2
    assert session.calls[0][2]["headers"]["x-if-match"] == ETAG_1
    assert session.calls[0][2]["headers"]["x-allow-overwrite"] == "1"


def test_conditional_json_update_rejects_stale_etag_and_requires_overwrite():
    store = BlobStore(STORE_ID, session=Session(Response(status=412)))

    with pytest.raises(ValueError, match="overwrite"):
        store.put_json(JOB_PATH, {"state": "running"}, if_match=ETAG_1)
    with pytest.raises(BlobConflictError):
        store.put_json(
            JOB_PATH,
            {"state": "running"},
            allow_overwrite=True,
            if_match=ETAG_1,
        )


@pytest.mark.parametrize("failure", ["oidc", "transport", "response"])
def test_blob_boundary_errors_suppress_sensitive_exception_chains(monkeypatch, failure):
    if failure == "oidc":
        monkeypatch.setattr(
            blob_store,
            "_get_oidc_token",
            lambda: (_ for _ in ()).throw(RuntimeError("oidc=super-secret")),
        )
        store = BlobStore(STORE_ID, session=Session(Response(chunks=[b"x"])))
        operation = lambda: store.get_bytes(INPUT, 10)
    elif failure == "transport":
        store = BlobStore(STORE_ID, session=RaisingSession())
        operation = lambda: store.get_bytes(INPUT, 10)
    else:
        store = BlobStore(
            STORE_ID,
            session=Session(
                InvalidJsonResponse(headers={"Content-Type": "application/json"})
            ),
        )
        operation = lambda: store.put_bytes(INPUT_PATH, b"x", "application/pdf")

    with pytest.raises(RuntimeError) as caught:
        operation()

    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert "super-secret" not in str(caught.value)
    assert "provider" not in str(caught.value).lower()


def test_stream_value_error_is_redacted_instead_of_escaping():
    store = BlobStore(STORE_ID, session=Session(RaisingStreamResponse()))
    with pytest.raises(RuntimeError, match="unavailable") as caught:
        store.get_bytes(INPUT, 10)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert "super-secret" not in str(caught.value)
