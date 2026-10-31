from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import pytest
from pydantic import ValidationError

from notesbot import config, mathpix_client
from notesbot.errors import VisualFidelityError
from notesbot.mathpix_client import (
    MathpixEvidence,
    MathpixLine,
    MathpixPage,
    _collect_mathpix_evidence_async,
    collect_mathpix_evidence,
    serialize_mathpix_evidence,
)
from notesbot.pdf_utils import PdfChunk
from notesbot.visual_manifest import VisualRunBudget


PDF_ID = "private/id secret"


class FakeContent:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        before_yield=None,
        hang: bool = False,
        started: asyncio.Event | None = None,
    ):
        self.chunks = chunks
        self.before_yield = before_yield
        self.hang = hang
        self.started = started
        self.cancelled = False
        self.completed = False

    async def iter_chunked(self, _size: int) -> AsyncIterator[bytes]:
        if self.hang:
            if self.started is not None:
                self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True
        for chunk in self.chunks:
            if self.before_yield is not None:
                self.before_yield()
            yield chunk
        self.completed = True


class FakeResponse:
    def __init__(
        self,
        status: int,
        body: bytes,
        *,
        headers: dict[str, str] | None = None,
        before_yield=None,
        hang_body: bool = False,
        body_started: asyncio.Event | None = None,
    ):
        self.status = status
        self.headers = headers or {"Content-Length": str(len(body))}
        self.content = FakeContent(
            [body], before_yield=before_yield, hang=hang_body, started=body_started
        )


class RequestContext:
    def __init__(
        self,
        response: FakeResponse | BaseException,
        *,
        hang_enter: bool = False,
        on_exit=None,
    ):
        self.response = response
        self.hang_enter = hang_enter
        self.on_exit = on_exit
        self.entered = False
        self.enter_cancelled = False
        self.exited = False

    async def __aenter__(self):
        if self.hang_enter:
            try:
                await asyncio.Event().wait()
            finally:
                self.enter_cancelled = True
        if isinstance(self.response, BaseException):
            raise self.response
        self.entered = True
        return self.response

    async def __aexit__(self, *_args):
        self.exited = True
        if self.on_exit is not None:
            self.on_exit()


@dataclass
class FakeSession:
    responses: list[RequestContext]
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    entered: int = 0
    exited: int = 0

    async def __aenter__(self):
        self.entered += 1
        return self

    async def __aexit__(self, *_args):
        self.exited += 1

    def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        if method == "DELETE":
            return self.responses.pop()
        return self.responses.pop(0)


def _j(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def _line(
    line_type: str = "diagram",
    *,
    text: str = "source label",
    cnt: list[dict[str, float]] | None = None,
    confidence: float | None = 0.95,
    line_no: int = 1,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": f"line-{line_no}",
        "line": line_no,
        "type": line_type,
        "text": text,
        "region": {"top_left_x": 10, "top_left_y": 20, "width": 40, "height": 30},
    }
    if cnt is not None:
        value["cnt"] = cnt
    if confidence is not None:
        value["confidence"] = confidence
    return value


def _lines(*lines: dict[str, Any], page_count: int = 1) -> bytes:
    pages = []
    for page in range(1, page_count + 1):
        pages.append(
            {
                "page": page,
                "page_width": 200,
                "page_height": 100,
                "lines": list(lines) if page == 1 else [_line(text=f"page {page}")],
            }
        )
    return _j({"pages": pages})


def _responses(
    *,
    lines: bytes | None = None,
    mmd: bytes = b"diagram source label",
    polls: list[str] | None = None,
) -> list[RequestContext]:
    result = [RequestContext(FakeResponse(200, _j({"pdf_id": PDF_ID})))]
    for state in polls or ["completed"]:
        result.append(RequestContext(FakeResponse(200, _j({"pdf_id": PDF_ID, "status": state}))))
    if (polls or ["completed"])[-1] == "completed":
        result.extend(
            [
                RequestContext(FakeResponse(200, lines or _lines(_line()))),
                RequestContext(FakeResponse(200, mmd)),
            ]
        )
    result.append(RequestContext(FakeResponse(200, _j({"pdf_id": PDF_ID, "status": "completed"}))))
    return result


def _chunk(*, pages: int = 1) -> PdfChunk:
    return PdfChunk(
        index=2,
        pdf_bytes=b"%PDF-1.7 bounded chunk",
        first_global_page=10,
        page_count=pages,
        page_geometries=(),
    )


def _budget(clock=None, deadline: float = 650.0) -> VisualRunBudget:
    return VisualRunBudget(deadline_at=deadline, clock=clock or (lambda: 1.0))


def _configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "MATHPIX_ENABLED", True)
    monkeypatch.setattr(config, "MATHPIX_APP_ID", "server-app-id")
    monkeypatch.setattr(config, "MATHPIX_APP_KEY", "server-app-key")
    monkeypatch.setattr(config, "MATHPIX_POLL_INTERVAL_SECONDS", 0.0)


def _install_hanging_native_dns(monkeypatch: pytest.MonkeyPatch):
    instances = []
    threaded_lookups = []

    def blocked_getaddrinfo(*args, **_kwargs):
        threaded_lookups.append(args[0] if args else None)
        time.sleep(0.30)
        raise socket.gaierror(socket.EAI_AGAIN, "threaded DNS must not run")

    class HangingNativeResolver(aiohttp.abc.AbstractResolver):
        def __init__(self, deadline_at: float, *, monotonic=time.monotonic) -> None:
            self.deadline_at = deadline_at
            self.created_at = monotonic()
            self.resolve_task = None
            self.cancelled = False
            self.closed = False
            instances.append(self)

        async def resolve(self, _host, port=0, family=socket.AF_UNSPEC):
            del port, family
            self.resolve_task = asyncio.current_task()
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(socket, "getaddrinfo", blocked_getaddrinfo)
    monkeypatch.setattr(
        mathpix_client,
        "AsyncDnsResolver",
        HangingNativeResolver,
        raising=False,
    )
    return instances, threaded_lookups


def _real_deadline_budget() -> VisualRunBudget:
    return VisualRunBudget(
        deadline_at=time.monotonic() + 5.0,
        clock=time.monotonic,
    )


def test_mathpix_default_session_submit_dns_cancels_within_request_deadline(
    monkeypatch,
):
    _configured(monkeypatch)
    monkeypatch.setattr(config, "MATHPIX_REQUEST_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(config, "MATHPIX_CLEANUP_RESERVE_SECONDS", 0.01)
    resolvers, threaded_lookups = _install_hanging_native_dns(monkeypatch)
    budget = _real_deadline_budget()

    started = time.perf_counter()
    with pytest.raises(VisualFidelityError) as caught:
        collect_mathpix_evidence(_chunk(), budget)
    elapsed = time.perf_counter() - started

    assert caught.value.code == "mathpix_submit_unconfirmed"
    assert elapsed < 0.15, f"submit crossed its hard bound: {elapsed:.3f}s"
    assert threaded_lookups == []
    assert len(resolvers) == 1
    assert resolvers[0].deadline_at == pytest.approx(budget.deadline_at - 0.01)
    assert resolvers[0].cancelled and resolvers[0].closed
    assert resolvers[0].resolve_task is not None
    assert resolvers[0].resolve_task.done()


def test_mathpix_default_session_delete_dns_cancels_within_cleanup_deadline(
    monkeypatch,
):
    _configured(monkeypatch)
    monkeypatch.setattr(config, "MATHPIX_CLEANUP_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(config, "MATHPIX_CLEANUP_RESERVE_SECONDS", 0.01)
    resolvers, threaded_lookups = _install_hanging_native_dns(monkeypatch)
    budget = _real_deadline_budget()

    async def submitted(*_args, **_kwargs):
        return PDF_ID

    async def incomplete(*_args, **_kwargs):
        return False

    monkeypatch.setattr(mathpix_client, "_submit", submitted)
    monkeypatch.setattr(mathpix_client, "_poll_until_complete", incomplete)
    started = time.perf_counter()
    with pytest.raises(VisualFidelityError) as caught:
        collect_mathpix_evidence(_chunk(), budget)
    elapsed = time.perf_counter() - started

    assert caught.value.code == "mathpix_cleanup_failed"
    assert elapsed < 0.15, f"cleanup crossed its hard bound: {elapsed:.3f}s"
    assert threaded_lookups == []
    assert len(resolvers) == 2
    work_resolver, cleanup_resolver = resolvers
    assert work_resolver.deadline_at == pytest.approx(budget.deadline_at - 0.01)
    assert work_resolver.resolve_task is None
    assert not work_resolver.cancelled and work_resolver.closed
    assert 0.0 < cleanup_resolver.deadline_at - cleanup_resolver.created_at <= 0.03
    assert cleanup_resolver.cancelled and cleanup_resolver.closed
    assert cleanup_resolver.resolve_task is not None
    assert cleanup_resolver.resolve_task.done()


def test_mathpix_cleanup_uses_one_absolute_deadline_after_session_entry(
    monkeypatch,
):
    _configured(monkeypatch)
    monkeypatch.setattr(config, "MATHPIX_CLEANUP_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(config, "MATHPIX_CLEANUP_RESERVE_SECONDS", 0.01)
    now = [1.0]
    entries = 0
    cleanup_response = FakeResponse(
        200,
        _j({"pdf_id": PDF_ID, "status": "completed"}),
        before_yield=lambda: now.__setitem__(0, 6.1),
    )
    cleanup_context = RequestContext(cleanup_response)
    cleanup_session = FakeSession([cleanup_context])

    @asynccontextmanager
    async def stepped_default_session():
        nonlocal entries
        entries += 1
        if entries == 1:
            yield FakeSession([])
            return
        now[0] = 5.5
        yield cleanup_session

    async def submitted(*_args, **_kwargs):
        return PDF_ID

    async def incomplete(*_args, **_kwargs):
        return False

    monkeypatch.setattr(mathpix_client, "_default_session", stepped_default_session)
    monkeypatch.setattr(mathpix_client, "_submit", submitted)
    monkeypatch.setattr(mathpix_client, "_poll_until_complete", incomplete)

    with pytest.raises(VisualFidelityError) as caught:
        collect_mathpix_evidence(
            _chunk(),
            _budget(clock=lambda: now[0], deadline=100.0),
        )

    assert caught.value.code == "mathpix_cleanup_failed"
    assert entries == 2
    assert [method for method, _url, _kwargs in cleanup_session.calls] == [
        "DELETE"
    ]
    assert not cleanup_response.content.completed
    assert cleanup_context.entered and cleanup_context.exited


def test_mathpix_cleanup_teardown_cancels_within_same_absolute_deadline(
    monkeypatch,
):
    _configured(monkeypatch)
    monkeypatch.setattr(config, "MATHPIX_CLEANUP_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr(config, "MATHPIX_CLEANUP_RESERVE_SECONDS", 0.01)
    entries = 0
    teardown_started = False
    teardown_cancelled = False

    @asynccontextmanager
    async def blocking_default_session():
        nonlocal entries, teardown_started, teardown_cancelled
        entries += 1
        try:
            yield FakeSession([])
        finally:
            if entries == 2:
                teardown_started = True
                try:
                    await asyncio.sleep(0.30)
                except asyncio.CancelledError:
                    teardown_cancelled = True
                    raise

    async def submitted(*_args, **_kwargs):
        return PDF_ID

    async def incomplete(*_args, **_kwargs):
        return False

    async def deleted(*_args, **_kwargs):
        return None

    monkeypatch.setattr(mathpix_client, "_default_session", blocking_default_session)
    monkeypatch.setattr(mathpix_client, "_submit", submitted)
    monkeypatch.setattr(mathpix_client, "_poll_until_complete", incomplete)
    monkeypatch.setattr(mathpix_client, "_delete", deleted)

    started = time.perf_counter()
    with pytest.raises(VisualFidelityError) as caught:
        collect_mathpix_evidence(_chunk(), _real_deadline_budget())
    elapsed = time.perf_counter() - started

    assert caught.value.code == "mathpix_cleanup_failed"
    assert elapsed < 0.15, f"teardown crossed cleanup bound: {elapsed:.3f}s"
    assert entries == 2
    assert teardown_started and teardown_cancelled


@pytest.mark.parametrize(
    ("submitted_id", "expected_code", "expected_entries"),
    (
        (None, "mathpix_submit_unconfirmed", 1),
        (PDF_ID, "mathpix_cleanup_failed", 2),
    ),
)
def test_mathpix_default_work_session_close_error_has_fixed_boundary_category(
    monkeypatch,
    capsys,
    submitted_id,
    expected_code,
    expected_entries,
):
    _configured(monkeypatch)
    monkeypatch.setattr(config, "MATHPIX_CLEANUP_RESERVE_SECONDS", 0.01)
    entries = 0
    cleanup_attempted = False

    @asynccontextmanager
    async def close_failing_work_session():
        nonlocal entries
        entries += 1
        if entries == 1:
            try:
                yield FakeSession([])
            finally:
                raise RuntimeError("sensitive work-session close failure")
        else:
            yield FakeSession([])

    async def submitted(*_args, **_kwargs):
        return submitted_id

    async def incomplete(*_args, **_kwargs):
        return False

    async def deleted(*_args, **_kwargs):
        nonlocal cleanup_attempted
        cleanup_attempted = True

    monkeypatch.setattr(
        mathpix_client,
        "_default_session",
        close_failing_work_session,
    )
    monkeypatch.setattr(mathpix_client, "_submit", submitted)
    monkeypatch.setattr(mathpix_client, "_poll_until_complete", incomplete)
    monkeypatch.setattr(mathpix_client, "_delete", deleted)

    with pytest.raises(VisualFidelityError) as caught:
        collect_mathpix_evidence(_chunk(), _real_deadline_budget())

    assert caught.value.code == expected_code
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert entries == expected_entries
    assert cleanup_attempted is (submitted_id is not None)
    output = capsys.readouterr().out
    if submitted_id is None:
        output_line = output.strip()
        assert output_line.startswith(
            "[notesbot.mathpix-error] stage=submit category=session_exit"
        )
        assert (
            " exc=notesbot.mathpix_client._DefaultSessionLifecycleError"
            "<-builtins.RuntimeError"
        ) in output_line
        assert "sensitive" not in output


def test_mathpix_default_work_session_entry_error_has_fixed_redacted_category(
    monkeypatch,
    capsys,
):
    _configured(monkeypatch)
    monkeypatch.setattr(config, "MATHPIX_CLEANUP_RESERVE_SECONDS", 0.01)

    @asynccontextmanager
    async def entry_failing_session():
        raise RuntimeError("TOP SECRET ENTRY FAILURE")
        yield FakeSession([])

    monkeypatch.setattr(mathpix_client, "_default_session", entry_failing_session)

    with pytest.raises(VisualFidelityError) as caught:
        collect_mathpix_evidence(_chunk(), _real_deadline_budget())

    assert caught.value.code == "mathpix_submit_unconfirmed"
    output_line = capsys.readouterr().out.strip()
    assert output_line.startswith(
        "[notesbot.mathpix-error] stage=submit category=session_entry"
    )
    assert (
        " exc=notesbot.mathpix_client._DefaultSessionLifecycleError"
        "<-builtins.RuntimeError"
    ) in output_line
    assert "TOP SECRET" not in output_line


def test_mathpix_default_resolver_init_error_uses_fixed_redacted_fallback(
    monkeypatch,
    capsys,
):
    _configured(monkeypatch)
    monkeypatch.setattr(config, "MATHPIX_CLEANUP_RESERVE_SECONDS", 0.01)
    closed: list[str] = []

    def resolver_init_failure(*_args, **_kwargs):
        raise RuntimeError("TOP SECRET RESOLVER FAILURE")

    class FallbackResolver:
        async def close(self):
            closed.append("resolver")

    class FallbackConnector:
        def __init__(self, **kwargs):
            assert isinstance(kwargs["resolver"], FallbackResolver)

        async def close(self):
            closed.append("connector")

    class FallbackClientSession:
        def __init__(self, **kwargs):
            assert isinstance(kwargs["connector"], FallbackConnector)

        async def __aenter__(self):
            return FakeSession([])

        async def __aexit__(self, *_exc_info):
            return False

    async def submitted(*_args, **_kwargs):
        return None

    monkeypatch.setattr(mathpix_client, "AsyncDnsResolver", resolver_init_failure)
    monkeypatch.setattr(aiohttp, "ThreadedResolver", FallbackResolver)
    monkeypatch.setattr(aiohttp, "TCPConnector", FallbackConnector)
    monkeypatch.setattr(aiohttp, "ClientSession", FallbackClientSession)
    monkeypatch.setattr(mathpix_client, "_submit", submitted)

    assert collect_mathpix_evidence(_chunk(), _real_deadline_budget()) is None

    output_line = capsys.readouterr().out.strip()
    assert output_line == (
        "[notesbot.mathpix-error] stage=session_init category=resolver_fallback"
        " exc=builtins.RuntimeError"
    )
    assert "TOP SECRET" not in output_line
    assert closed == ["connector", "resolver"]


def _collect(monkeypatch: pytest.MonkeyPatch, session: FakeSession, *, chunk=None, budget=None):
    _configured(monkeypatch)
    return collect_mathpix_evidence(
        chunk or _chunk(),
        budget or _budget(),
        session_factory=lambda: session,
        sleeper=lambda _seconds: asyncio.sleep(0),
    )


def _assert_owned_resources_closed(
    session: FakeSession, contexts: list[RequestContext]
) -> None:
    assert session.entered == session.exited == 1
    assert all(not context.entered or context.exited for context in contexts)


def test_mathpix_disabled_missing_or_partial_credentials_make_zero_http_calls(monkeypatch):
    session = FakeSession([])
    monkeypatch.setattr(config, "MATHPIX_ENABLED", False)
    monkeypatch.setattr(config, "MATHPIX_APP_ID", "server-app-id")
    monkeypatch.setattr(config, "MATHPIX_APP_KEY", "server-app-key")
    assert collect_mathpix_evidence(_chunk(), _budget(), session_factory=lambda: session) is None
    monkeypatch.setattr(config, "MATHPIX_ENABLED", True)
    for app_id, app_key in [("", ""), ("only-id", ""), ("", "only-key")]:
        monkeypatch.setattr(config, "MATHPIX_APP_ID", app_id)
        monkeypatch.setattr(config, "MATHPIX_APP_KEY", app_key)
        assert collect_mathpix_evidence(_chunk(), _budget(), session_factory=lambda: session) is None
    assert session.calls == []


def test_mathpix_submit_uses_server_auth_generic_filename_and_private_options(monkeypatch):
    session = FakeSession(_responses())
    assert _collect(monkeypatch, session) is not None
    method, url, kwargs = session.calls[0]
    assert (method, url, kwargs["allow_redirects"]) == ("POST", "https://api.mathpix.com/v3/pdf", False)
    assert kwargs["headers"] == {"app_id": "server-app-id", "app_key": "server-app-key"}
    fields = kwargs["data"]._fields
    assert any(field.get("filename") == "lecture-chunk.pdf" and value == _chunk().pdf_bytes for field, _h, value in fields)
    options = next(value for field, _h, value in fields if field["name"] == "options_json")
    assert json.loads(options) == {
        "metadata": {"improve_mathpix": False},
        "include_smiles": True,
        "include_page_breaks": True,
        "preserve_section_numbering": True,
    }
    assert "private" not in repr(fields).lower()


def test_mathpix_submit_connect_failure_returns_none_for_gemini_fallback(monkeypatch):
    error = aiohttp.ClientConnectorError.__new__(aiohttp.ClientConnectorError)
    session = FakeSession([RequestContext(error)])
    assert _collect(monkeypatch, session) is None
    assert len(session.calls) == 1


def test_mathpix_submit_4xx_falls_back_but_5xx_fails_privacy_closed(monkeypatch):
    assert _collect(monkeypatch, FakeSession([RequestContext(FakeResponse(429, b"denied"))])) is None
    monkeypatch.setattr(config, "MATHPIX_MAX_RESPONSE_BYTES", 4)
    oversized_refusal = FakeResponse(429, b"denied")
    assert _collect(monkeypatch, FakeSession([RequestContext(oversized_refusal)])) is None
    with pytest.raises(VisualFidelityError) as caught:
        _collect(monkeypatch, FakeSession([RequestContext(FakeResponse(503, b"later"))]))
    assert caught.value.code == "mathpix_submit_unconfirmed"


def test_mathpix_submit_3xx_redirect_is_unconfirmed_and_never_followed(monkeypatch):
    session = FakeSession([RequestContext(FakeResponse(302, b"redirect", headers={"Location": "https://evil.invalid"}))])
    with pytest.raises(VisualFidelityError) as caught:
        _collect(monkeypatch, session)
    assert caught.value.code == "mathpix_submit_unconfirmed"
    assert len(session.calls) == 1 and session.calls[0][2]["allow_redirects"] is False


def test_mathpix_submit_read_timeout_or_success_without_pdf_id_fails_privacy_closed(monkeypatch):
    monkeypatch.setattr(config, "MATHPIX_REQUEST_TIMEOUT_SECONDS", 0.01)
    hanging = FakeSession([RequestContext(FakeResponse(200, b"", hang_body=True))])
    with pytest.raises(VisualFidelityError, match="temporary-data state"):
        _collect(monkeypatch, hanging)
    with pytest.raises(VisualFidelityError) as caught:
        _collect(monkeypatch, FakeSession([RequestContext(FakeResponse(200, _j({"status": "received"})))]))
    assert caught.value.code == "mathpix_submit_unconfirmed"


@pytest.mark.parametrize(
    ("response", "expected_category", "expected_suffix"),
    [
        pytest.param(FakeResponse(503, b"TOP SECRET SERVER BODY"), "server_response", "", id="server"),
        pytest.param(FakeResponse(200, b"TOP SECRET INVALID JSON"), "invalid_response", "", id="json"),
        pytest.param(
            FakeResponse(200, _j({"status": "received", "secret": "TOP SECRET"})),
            "missing_pdf_id",
            "",
            id="missing-id",
        ),
        pytest.param(
            aiohttp.ClientPayloadError("TOP SECRET TRANSPORT"),
            "transport_error",
            " exc=aiohttp.client_exceptions.ClientPayloadError",
            id="transport",
        ),
        pytest.param(
            aiohttp.ConnectionTimeoutError("TOP SECRET CONNECT TIMEOUT"),
            "transport_error",
            " exc=aiohttp.client_exceptions.ConnectionTimeoutError",
            id="connect-timeout",
        ),
    ],
)
def test_mathpix_submit_failure_event_is_allowlisted_and_redacted(
    monkeypatch,
    capsys,
    response,
    expected_category,
    expected_suffix,
):
    with pytest.raises(VisualFidelityError) as caught:
        _collect(monkeypatch, FakeSession([RequestContext(response)]))

    assert caught.value.code == "mathpix_submit_unconfirmed"
    output_line = capsys.readouterr().out.strip()
    assert output_line == (
        f"[notesbot.mathpix-error] stage=submit category={expected_category}"
        f"{expected_suffix}"
    )
    assert "TOP SECRET" not in output_line


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            f'{{"pdf_id":{json.dumps(PDF_ID)},"pdf_id":{json.dumps(PDF_ID)}}}'.encode(),
            id="identical-top-level",
        ),
        pytest.param(
            f'{{"pdf_id":"discarded-id","pdf_id":{json.dumps(PDF_ID)}}}'.encode(),
            id="differing-top-level",
        ),
        pytest.param(
            f'{{"pdf_id":{json.dumps(PDF_ID)},"metadata":{{"token":1,"token":1}}}}'.encode(),
            id="identical-nested",
        ),
        pytest.param(
            f'{{"pdf_id":{json.dumps(PDF_ID)},"metadata":{{"token":1,"token":2}}}}'.encode(),
            id="differing-nested",
        ),
    ],
)
def test_mathpix_submit_duplicate_json_keys_are_unconfirmed_without_cleanup(
    monkeypatch, body
):
    responses = _responses()
    responses[0] = RequestContext(FakeResponse(200, body))
    contexts = list(responses)
    session = FakeSession(responses)

    with pytest.raises(VisualFidelityError) as caught:
        _collect(monkeypatch, session)

    assert caught.value.code == "mathpix_submit_unconfirmed"
    assert str(caught.value) == VisualFidelityError.MESSAGES["mathpix_submit_unconfirmed"]
    assert [method for method, _url, _kwargs in session.calls] == ["POST"]
    assert session.entered == session.exited == 1
    assert all(not context.entered or context.exited for context in contexts)


@pytest.mark.parametrize(
    "poll_body",
    [
        pytest.param(
            f'{{"pdf_id":{json.dumps(PDF_ID)},"status":"completed","status":"completed"}}'.encode(),
            id="identical-top-level",
        ),
        pytest.param(
            f'{{"pdf_id":{json.dumps(PDF_ID)},"status":"error","status":"completed"}}'.encode(),
            id="differing-top-level",
        ),
        pytest.param(
            f'{{"pdf_id":{json.dumps(PDF_ID)},"status":"completed","metadata":{{"token":1,"token":2}}}}'.encode(),
            id="nested",
        ),
    ],
)
def test_mathpix_poll_duplicate_json_keys_use_bounded_fallback_then_delete_once(
    monkeypatch, poll_body
):
    responses = _responses()
    responses[1] = RequestContext(FakeResponse(200, poll_body))
    contexts = list(responses)
    session = FakeSession(responses)

    assert _collect(monkeypatch, session) is None
    assert [method for method, _url, _kwargs in session.calls] == [
        "POST",
        "GET",
        "DELETE",
    ]
    assert session.calls[-1][1].endswith("private%2Fid%20secret")
    _assert_owned_resources_closed(session, contexts)


@pytest.mark.parametrize(
    "duplicate_member",
    [
        pytest.param(b'"token":1,"token":1', id="identical-nested"),
        pytest.param(b'"token":1,"token":2', id="differing-nested"),
    ],
)
def test_mathpix_lines_download_duplicate_json_keys_falls_back_then_deletes_once(
    monkeypatch, duplicate_member
):
    lines_body = _lines(_line()).replace(
        b'{"pages":',
        b'{"metadata":{' + duplicate_member + b'},"pages":',
        1,
    )
    responses = _responses(lines=lines_body)
    contexts = list(responses)
    session = FakeSession(responses)

    assert _collect(monkeypatch, session) is None
    assert [method for method, _url, _kwargs in session.calls] == [
        "POST",
        "GET",
        "GET",
        "GET",
        "DELETE",
    ]
    assert [method for method, _url, _kwargs in session.calls].count("DELETE") == 1
    _assert_owned_resources_closed(session, contexts)


def test_mathpix_poll_is_bounded_by_attempt_count_deadline_and_cleanup_reserve(monkeypatch):
    monkeypatch.setattr(config, "MATHPIX_MAX_POLL_ATTEMPTS", 2)
    session = FakeSession(_responses(polls=["received", "loaded"]))
    assert _collect(monkeypatch, session) is None
    assert [method for method, _url, _kwargs in session.calls] == ["POST", "GET", "GET", "DELETE"]
    no_time = FakeSession([])
    _configured(monkeypatch)
    assert collect_mathpix_evidence(_chunk(), _budget(deadline=10.9), session_factory=lambda: no_time) is None
    assert no_time.calls == []


def test_mathpix_slow_drip_submit_poll_and_download_streams_cannot_cross_work_deadline(monkeypatch):
    expected_methods = {
        0: ["POST"],
        1: ["POST", "GET", "DELETE"],
        2: ["POST", "GET", "GET", "DELETE"],
        3: ["POST", "GET", "GET", "GET", "DELETE"],
    }
    for position in (0, 1, 2, 3):
        now = [1.0]
        responses = _responses()
        contexts = list(responses)
        response = responses[position].response
        assert isinstance(response, FakeResponse)
        response.content.before_yield = lambda: now.__setitem__(0, 641.0)
        session = FakeSession(responses)
        if position == 0:
            with pytest.raises(VisualFidelityError) as caught:
                _collect(monkeypatch, session, budget=_budget(clock=lambda: now[0]))
            assert caught.value.code == "mathpix_submit_unconfirmed"
        else:
            assert _collect(monkeypatch, session, budget=_budget(clock=lambda: now[0])) is None
            assert session.calls[-1][0] == "DELETE"
        assert [method for method, _url, _kwargs in session.calls] == expected_methods[position]
        assert [method for method, _url, _kwargs in session.calls].count("DELETE") == (
            0 if position == 0 else 1
        )
        _assert_owned_resources_closed(session, contexts)


def test_mathpix_total_timeout_cancels_upload_dns_and_stream_before_first_chunk_yield(monkeypatch):
    monkeypatch.setattr(config, "MATHPIX_REQUEST_TIMEOUT_SECONDS", 0.01)
    context = RequestContext(FakeResponse(200, b""), hang_enter=True)
    session = FakeSession([context])
    with pytest.raises(VisualFidelityError) as caught:
        _collect(monkeypatch, session)
    assert caught.value.code == "mathpix_submit_unconfirmed"
    assert [method for method, _url, _kwargs in session.calls] == ["POST"]
    assert context.enter_cancelled
    assert session.entered == session.exited == 1


def test_mathpix_post_dispatch_cancellation_is_privacy_closed_and_leaves_no_task(monkeypatch):
    _configured(monkeypatch)

    async def invoke() -> None:
        body_started = asyncio.Event()
        response = FakeResponse(
            200,
            _j({"pdf_id": PDF_ID}),
            hang_body=True,
            body_started=body_started,
        )
        context = RequestContext(response)
        session = FakeSession([context])
        before = set(asyncio.all_tasks())
        task = asyncio.create_task(
            _collect_mathpix_evidence_async(
                _chunk(),
                _budget(),
                session_factory=lambda: session,
                sleeper=asyncio.sleep,
            )
        )
        await asyncio.wait_for(body_started.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(VisualFidelityError) as caught:
            await task
        assert caught.value.code == "mathpix_submit_unconfirmed"
        assert [method for method, _url, _kwargs in session.calls] == ["POST"]
        assert context.entered and context.exited
        assert response.content.cancelled
        assert session.entered == session.exited == 1

        unexpected_context = RequestContext(RuntimeError("sensitive post-dispatch failure"))
        unexpected_session = FakeSession([unexpected_context])
        with pytest.raises(VisualFidelityError) as unexpected_error:
            await _collect_mathpix_evidence_async(
                _chunk(),
                _budget(),
                session_factory=lambda: unexpected_session,
                sleeper=asyncio.sleep,
            )
        assert unexpected_error.value.code == "mathpix_submit_unconfirmed"
        assert unexpected_error.value.__cause__ is None
        assert unexpected_error.value.__suppress_context__
        assert [method for method, _url, _kwargs in unexpected_session.calls] == ["POST"]
        assert unexpected_session.entered == unexpected_session.exited == 1
        await asyncio.sleep(0)
        assert set(asyncio.all_tasks()) == before

    asyncio.run(invoke())


def test_mathpix_sync_runner_rejects_existing_event_loop_before_network_and_leaves_no_task(monkeypatch):
    session = FakeSession([])
    _configured(monkeypatch)

    async def invoke():
        before = set(asyncio.all_tasks())
        with pytest.raises(RuntimeError, match="mathpix_event_loop_active"):
            collect_mathpix_evidence(_chunk(), _budget(), session_factory=lambda: session)
        assert set(asyncio.all_tasks()) == before

    asyncio.run(invoke())
    assert session.calls == []


def test_mathpix_provider_does_not_record_outcomes_or_suppress_budget_errors(monkeypatch):
    success_budget = _budget()
    assert _collect(monkeypatch, FakeSession(_responses()), budget=success_budget) is not None
    assert success_budget.mathpix_evidence_used == {}
    assert success_budget.mathpix_source_fallback == {}

    work_budget = _budget()
    work_budget.mathpix_poll_calls[_chunk().index] = config.MATHPIX_MAX_POLL_ATTEMPTS
    work_session = FakeSession(_responses())
    with pytest.raises(VisualFidelityError) as work_error:
        _collect(monkeypatch, work_session, budget=work_budget)
    assert work_error.value.code == "work_limit_exceeded"
    assert [method for method, _url, _kwargs in work_session.calls] == ["POST", "DELETE"]
    assert work_budget.mathpix_evidence_used == {}
    assert work_budget.mathpix_source_fallback == {}

    now = [1.0]
    deadline_budget = _budget(clock=lambda: now[0])
    deadline_responses = _responses()
    deadline_responses[0].on_exit = lambda: now.__setitem__(0, 650.0)
    deadline_session = FakeSession(deadline_responses)
    with pytest.raises(VisualFidelityError) as deadline_error:
        _collect(monkeypatch, deadline_session, budget=deadline_budget)
    assert deadline_error.value.code == "deadline_exceeded"
    assert [method for method, _url, _kwargs in deadline_session.calls] == ["POST", "DELETE"]
    assert deadline_budget.mathpix_evidence_used == {}
    assert deadline_budget.mathpix_source_fallback == {}

    delete_budget = _budget()
    delete_budget.mathpix_delete_calls[_chunk().index] = 1
    delete_session = FakeSession(_responses())
    with pytest.raises(VisualFidelityError) as delete_error:
        _collect(monkeypatch, delete_session, budget=delete_budget)
    assert delete_error.value.code == "work_limit_exceeded"
    assert [method for method, _url, _kwargs in delete_session.calls] == [
        "POST", "GET", "GET", "GET"
    ]


def test_mathpix_received_loaded_split_then_completed_downloads_lines_and_mmd_once(monkeypatch):
    session = FakeSession(_responses(polls=["received", "loaded", "split", "completed"]))
    evidence = _collect(monkeypatch, session)
    assert evidence is not None
    urls = [url for _method, url, _kwargs in session.calls]
    assert sum(url.endswith(".lines.json") for url in urls) == 1
    assert sum(url.endswith(".mmd") for url in urls) == 1
    assert [method for method, _url, _kwargs in session.calls].count("DELETE") == 1


def test_mathpix_error_timeout_download_or_parse_failure_returns_none_after_delete(monkeypatch):
    monkeypatch.setattr(config, "MATHPIX_REQUEST_TIMEOUT_SECONDS", 0.01)

    poll_timeout = _responses()
    poll_timeout_response = FakeResponse(200, b"", hang_body=True)
    poll_timeout[1] = RequestContext(poll_timeout_response)

    lines_status = _responses()
    lines_status[2] = RequestContext(FakeResponse(503, b"download unavailable"))

    lines_transport = _responses()
    lines_transport[2] = RequestContext(aiohttp.ClientPayloadError("lines transport"))

    mmd_status = _responses()
    mmd_status[3] = RequestContext(FakeResponse(404, b"download unavailable"))

    mmd_transport = _responses()
    mmd_transport[3] = RequestContext(aiohttp.ClientPayloadError("mmd transport"))

    cases = [
        (_responses(polls=["error"]), ["POST", "GET", "DELETE"]),
        (poll_timeout, ["POST", "GET", "DELETE"]),
        (lines_status, ["POST", "GET", "GET", "DELETE"]),
        (lines_transport, ["POST", "GET", "GET", "DELETE"]),
        (mmd_status, ["POST", "GET", "GET", "GET", "DELETE"]),
        (mmd_transport, ["POST", "GET", "GET", "GET", "DELETE"]),
        (_responses(lines=b"not-json"), ["POST", "GET", "GET", "GET", "DELETE"]),
        (_responses(mmd=b"\xff"), ["POST", "GET", "GET", "GET", "DELETE"]),
    ]
    for responses, expected_methods in cases:
        contexts = list(responses)
        session = FakeSession(responses)
        assert _collect(monkeypatch, session) is None
        assert [method for method, _url, _kwargs in session.calls] == expected_methods
        assert [method for method, _url, _kwargs in session.calls].count("DELETE") == 1
        _assert_owned_resources_closed(session, contexts)
    assert poll_timeout_response.content.cancelled


def test_mathpix_every_response_is_stream_capped_before_json_or_text_parse(monkeypatch):
    monkeypatch.setattr(config, "MATHPIX_MAX_RESPONSE_BYTES", 512)
    expected_methods = {
        0: ["POST"],
        1: ["POST", "GET", "DELETE"],
        2: ["POST", "GET", "GET", "DELETE"],
        3: ["POST", "GET", "GET", "GET", "DELETE"],
        4: ["POST", "GET", "GET", "GET", "DELETE"],
    }
    for index in range(5):
        responses = _responses()
        contexts = list(responses)
        target = responses[index].response
        assert isinstance(target, FakeResponse)
        target.headers = {"Content-Length": "513"}
        session = FakeSession(responses)
        if index == 0:
            with pytest.raises(VisualFidelityError) as caught:
                _collect(monkeypatch, session)
            assert caught.value.code == "mathpix_submit_unconfirmed"
        elif index == 4:
            with pytest.raises(VisualFidelityError) as caught:
                _collect(monkeypatch, session)
            assert caught.value.code == "mathpix_cleanup_failed"
        else:
            assert _collect(monkeypatch, session) is None
        assert [method for method, _url, _kwargs in session.calls] == expected_methods[index]
        _assert_owned_resources_closed(session, contexts)


def test_mathpix_lines_pages_must_exactly_match_one_based_chunk_pages(monkeypatch):
    bad = _j({"pages": [{"page": 2, "page_width": 100, "page_height": 100, "lines": []}]})
    session = FakeSession(_responses(lines=bad))
    assert _collect(monkeypatch, session, chunk=_chunk(pages=1)) is None
    assert session.calls[-1][0] == "DELETE"


def test_mathpix_evidence_carries_exact_chunk_digest_offset_and_page_count(monkeypatch):
    chunk = _chunk()
    evidence = _collect(monkeypatch, FakeSession(_responses()), chunk=chunk)
    assert evidence is not None
    assert (evidence.chunk_index, evidence.chunk_sha256, evidence.first_global_page, evidence.page_count) == (
        chunk.index,
        chunk.sha256_hex,
        chunk.first_global_page,
        chunk.page_count,
    )
    payload = json.loads(serialize_mathpix_evidence(evidence, chunk))
    assert set(payload) == {"pages"}
    assert not ({"chunk_index", "chunk_sha256", "first_global_page", "page_count", "mmd_page_count"} & set(payload))


def test_mathpix_evidence_rejects_line_parent_page_mismatch() -> None:
    chunk = _chunk()
    mismatched_line = MathpixLine(
        local_page=2,
        box=(10.0, 20.0, 50.0, 50.0),
        polygon=(),
        line_type="diagram",
        role="visual_candidate",
    )
    page = MathpixPage(
        local_page=1,
        page_width=200,
        page_height=100,
        lines=(mismatched_line,),
    )
    values = {
        "chunk_index": chunk.index,
        "chunk_sha256": chunk.sha256_hex,
        "first_global_page": chunk.first_global_page,
        "page_count": chunk.page_count,
        "pages": (page,),
        "mmd_page_count": chunk.page_count,
    }
    with pytest.raises(ValidationError, match="mathpix_line_page_mismatch"):
        MathpixEvidence(**values)

    bypassed = MathpixEvidence.model_construct(**values)
    with pytest.raises(ValueError, match="mathpix_evidence_provenance_invalid"):
        serialize_mathpix_evidence(bypassed, chunk)


def test_mathpix_normalizes_region_and_cnt_to_page_percent_coordinates(monkeypatch):
    polygon = [{"x": 20, "y": 10}, {"x": 180, "y": 10}, {"x": 180, "y": 90}, {"x": 20, "y": 90}]
    evidence = _collect(monkeypatch, FakeSession(_responses(lines=_lines(_line(cnt=polygon)))))
    assert evidence is not None
    line = evidence.pages[0].lines[0]
    assert line.box == (10.0, 10.0, 90.0, 90.0)
    assert line.polygon == ((10.0, 10.0), (90.0, 10.0), (90.0, 90.0), (10.0, 90.0))


def test_mathpix_preserves_diagram_chart_axis_tick_legend_figure_label_and_chemistry_lines(monkeypatch):
    types = [
        "diagram", "chart", "chemistry", "chemistry_reaction", "diagram_info", "chart_info",
        "figure_label", "x_axis_tick_label", "y_axis_tick_label", "x_axis_label", "y_axis_label",
        "legend_label", "model_label",
    ]
    records = [_line(t, line_no=i + 1, text=t) for i, t in enumerate(types)]
    evidence = _collect(monkeypatch, FakeSession(_responses(lines=_lines(*records), mmd=" ".join(types).encode())))
    assert evidence is not None
    assert [line.line_type for line in evidence.pages[0].lines] == types


def test_mathpix_low_confidence_is_retained_as_uncertain_not_dropped(monkeypatch):
    evidence = _collect(monkeypatch, FakeSession(_responses(lines=_lines(_line(confidence=0.74)))))
    assert evidence is not None and evidence.pages[0].lines[0].uncertain is True


def test_mathpix_unknown_polygon_line_is_retained_as_uncertain_proposal(monkeypatch):
    cnt = [{"x": 20, "y": 20}, {"x": 40, "y": 40}, {"x": 60, "y": 20}]
    evidence = _collect(monkeypatch, FakeSession(_responses(lines=_lines(_line("new_provider_type", cnt=cnt)))))
    assert evidence is not None
    assert evidence.pages[0].lines[0].role == "uncertain"


def test_mathpix_cdn_and_other_urls_do_not_survive_normalized_evidence(monkeypatch):
    text = "![diagram](https://cdn.mathpix.com/raw.png) [visible](https://evil.invalid/x) https://bare.invalid/y"
    evidence = _collect(monkeypatch, FakeSession(_responses(lines=_lines(_line(text=text)), mmd=b"diagram visible")))
    assert evidence is not None
    payload = serialize_mathpix_evidence(evidence, _chunk())
    assert b"http://" not in payload and b"https://" not in payload
    assert b"diagram" in payload and b"visible" in payload


def test_mathpix_polygon_point_and_serialized_json_base64_caps_fail_to_source_fallback(monkeypatch):
    monkeypatch.setattr(config, "MATHPIX_MAX_POLYGON_POINTS_PER_LINE", 4)
    too_many = [{"x": float(i + 1), "y": float(i + 1)} for i in range(5)]
    assert _collect(monkeypatch, FakeSession(_responses(lines=_lines(_line(cnt=too_many))))) is None
    monkeypatch.setattr(config, "MATHPIX_MAX_EVIDENCE_JSON_BYTES", 80)
    monkeypatch.setattr(config, "MATHPIX_MAX_EVIDENCE_ENCODED_BYTES", 100)
    assert _collect(monkeypatch, FakeSession(_responses(lines=_lines(_line(text="x" * 200)), mmd=b"x" * 200))) is None


def test_mathpix_delete_runs_exactly_once_on_success_error_timeout_and_parse_failure(monkeypatch):
    monkeypatch.setattr(config, "MATHPIX_REQUEST_TIMEOUT_SECONDS", 0.01)
    timeout_responses = _responses()
    timeout_response = FakeResponse(200, b"", hang_body=True)
    timeout_responses[1] = RequestContext(timeout_response)
    cases = [
        (_responses(), True),
        (_responses(polls=["error"]), False),
        (timeout_responses, False),
        (_responses(lines=b"bad"), False),
    ]
    for responses, expects_evidence in cases:
        contexts = list(responses)
        session = FakeSession(responses)
        budget = _budget()
        result = _collect(monkeypatch, session, budget=budget)
        assert (result is not None) is expects_evidence
        assert [method for method, _url, _kwargs in session.calls].count("DELETE") == 1
        assert session.calls[0][0] == "POST" and session.calls[-1][0] == "DELETE"
        assert budget.mathpix_delete_calls == {_chunk().index: 1}
        _assert_owned_resources_closed(session, contexts)
    assert timeout_response.content.cancelled


def test_mathpix_delete_still_runs_after_main_deadline_expires(monkeypatch):
    now = [1.0]
    responses = _responses()
    poll = responses[1].response
    assert isinstance(poll, FakeResponse)
    poll.content.before_yield = lambda: now.__setitem__(0, 651.0)
    contexts = list(responses)
    session = FakeSession(responses)
    with pytest.raises(VisualFidelityError) as caught:
        _collect(monkeypatch, session, budget=_budget(clock=lambda: now[0]))
    assert caught.value.code == "deadline_exceeded"
    assert session.calls[-1][0] == "DELETE"
    assert [method for method, _url, _kwargs in session.calls].count("DELETE") == 1
    _assert_owned_resources_closed(session, contexts)


def test_mathpix_slow_drip_delete_stream_cannot_cross_cleanup_deadline(monkeypatch):
    now = [1.0]
    responses = _responses()
    deleted = responses[-1].response
    assert isinstance(deleted, FakeResponse)
    deleted.content.before_yield = lambda: now.__setitem__(0, 7.0)
    with pytest.raises(VisualFidelityError) as caught:
        _collect(monkeypatch, FakeSession(responses), budget=_budget(clock=lambda: now[0]))
    assert caught.value.code == "mathpix_cleanup_failed"


def test_mathpix_delete_total_timeout_cancels_before_first_body_chunk_yield(monkeypatch):
    monkeypatch.setattr(config, "MATHPIX_CLEANUP_TIMEOUT_SECONDS", 0.01)
    responses = _responses()
    delete_response = FakeResponse(200, b"", hang_body=True)
    responses[-1] = RequestContext(delete_response)
    contexts = list(responses)
    session = FakeSession(responses)
    with pytest.raises(VisualFidelityError) as caught:
        _collect(monkeypatch, session)
    assert caught.value.code == "mathpix_cleanup_failed"
    assert [method for method, _url, _kwargs in session.calls] == [
        "POST", "GET", "GET", "GET", "DELETE"
    ]
    assert delete_response.content.cancelled
    _assert_owned_resources_closed(session, contexts)


def test_mathpix_delete_failure_or_timeout_blocks_delivery_with_fixed_error(monkeypatch):
    monkeypatch.setattr(config, "MATHPIX_CLEANUP_TIMEOUT_SECONDS", 0.01)
    timeout_response = FakeResponse(200, b"", hang_body=True)
    cleanup_bodies: list[FakeResponse] = [
        FakeResponse(500, b"secret provider failure"),
        FakeResponse(200, b"not-json"),
        FakeResponse(200, _j({"pdf_id": "different-id", "status": "completed"})),
        FakeResponse(200, _j({"pdf_id": PDF_ID, "status": "unknown"})),
        timeout_response,
    ]
    for cleanup_response in cleanup_bodies:
        responses = _responses()
        responses[-1] = RequestContext(cleanup_response)
        contexts = list(responses)
        session = FakeSession(responses)
        with pytest.raises(VisualFidelityError) as caught:
            _collect(monkeypatch, session)
        assert caught.value.code == "mathpix_cleanup_failed"
        assert str(caught.value) == VisualFidelityError.MESSAGES["mathpix_cleanup_failed"]
        assert caught.value.__cause__ is None and caught.value.__suppress_context__
        assert [method for method, _url, _kwargs in session.calls] == [
            "POST", "GET", "GET", "GET", "DELETE"
        ]
        _assert_owned_resources_closed(session, contexts)
    assert timeout_response.content.cancelled


@pytest.mark.parametrize(
    "cleanup_body",
    [
        pytest.param(
            f'{{"pdf_id":{json.dumps(PDF_ID)},"pdf_id":{json.dumps(PDF_ID)},"status":"completed"}}'.encode(),
            id="identical-top-level",
        ),
        pytest.param(
            f'{{"pdf_id":"discarded-id","pdf_id":{json.dumps(PDF_ID)},"status":"completed"}}'.encode(),
            id="differing-top-level",
        ),
        pytest.param(
            f'{{"pdf_id":{json.dumps(PDF_ID)},"status":"completed","metadata":{{"token":1,"token":2}}}}'.encode(),
            id="nested",
        ),
    ],
)
def test_mathpix_cleanup_duplicate_json_keys_block_delivery_after_one_delete(
    monkeypatch, cleanup_body
):
    responses = _responses()
    responses[-1] = RequestContext(FakeResponse(200, cleanup_body))
    contexts = list(responses)
    session = FakeSession(responses)

    with pytest.raises(VisualFidelityError) as caught:
        _collect(monkeypatch, session)

    assert caught.value.code == "mathpix_cleanup_failed"
    assert str(caught.value) == VisualFidelityError.MESSAGES["mathpix_cleanup_failed"]
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert [method for method, _url, _kwargs in session.calls] == [
        "POST",
        "GET",
        "GET",
        "GET",
        "DELETE",
    ]
    assert [method for method, _url, _kwargs in session.calls].count("DELETE") == 1
    _assert_owned_resources_closed(session, contexts)


def test_mathpix_errors_and_logs_never_contain_credentials_pdf_id_or_ocr_text(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    sensitive = f"server-app-id server-app-key {PDF_ID} TOP SECRET OCR"
    rendered_errors: list[str] = []

    submit_session = FakeSession(
        [RequestContext(aiohttp.ClientPayloadError(sensitive))]
    )
    with pytest.raises(VisualFidelityError) as submit_error:
        _collect(monkeypatch, submit_session)
    assert submit_error.value.code == "mathpix_submit_unconfirmed"
    assert submit_error.value.__cause__ is None
    assert submit_error.value.__suppress_context__
    assert [method for method, _url, _kwargs in submit_session.calls] == ["POST"]
    assert submit_session.entered == submit_session.exited == 1
    rendered_errors.append(str(submit_error.value))

    stage_cases = [
        (1, ["POST", "GET", "DELETE"]),
        (2, ["POST", "GET", "GET", "DELETE"]),
        (3, ["POST", "GET", "GET", "GET", "DELETE"]),
    ]
    for position, expected_methods in stage_cases:
        responses = _responses(lines=_lines(_line(text="TOP SECRET OCR")))
        responses[position] = RequestContext(aiohttp.ClientPayloadError(sensitive))
        contexts = list(responses)
        session = FakeSession(responses)
        assert _collect(monkeypatch, session) is None
        assert [method for method, _url, _kwargs in session.calls] == expected_methods
        _assert_owned_resources_closed(session, contexts)

    cleanup_responses = _responses(lines=_lines(_line(text="TOP SECRET OCR")))
    cleanup_responses[-1] = RequestContext(FakeResponse(500, sensitive.encode()))
    cleanup_session = FakeSession(cleanup_responses)
    with pytest.raises(VisualFidelityError) as cleanup_error:
        _collect(monkeypatch, cleanup_session)
    assert cleanup_error.value.code == "mathpix_cleanup_failed"
    assert cleanup_error.value.__cause__ is None
    assert cleanup_error.value.__suppress_context__
    rendered_errors.append(str(cleanup_error.value))

    visible = "".join(rendered_errors) + caplog.text
    for secret in ("server-app-id", "server-app-key", PDF_ID, "TOP SECRET OCR"):
        assert secret not in visible
