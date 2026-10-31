"""Bounded, server-only Mathpix planning-evidence boundary."""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import AsyncIterable, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from types import TracebackType
from typing import Any, AsyncContextManager, Literal, Protocol
from urllib.parse import quote

import aiohttp
import dns.exception
from pydantic import Field, field_validator, model_validator

from . import config
from .async_dns import AsyncDnsResolver
from .errors import VisualFidelityError
from .pdf_utils import PdfChunk
from .visual_manifest import FrozenModel, VisualRunBudget

EvidenceRole = Literal[
    "visual_candidate", "label_candidate", "context_candidate", "uncertain"
]

_BASE_URL = "https://api.mathpix.com"
_POLL_STATES = frozenset({"received", "loaded", "split", "completed", "error"})
_VISUAL_TYPES = frozenset({"diagram", "chart", "chemistry", "chemistry_reaction"})
_LABEL_TYPES = frozenset(
    {
        "figure_label",
        "x_axis_tick_label",
        "y_axis_tick_label",
        "x_axis_label",
        "y_axis_label",
        "legend_label",
        "model_label",
    }
)
_CONTEXT_TYPES = frozenset(
    {"diagram_info", "chart_info", "section_header", "math", "text"}
)
_MMD_CORROBORATED_TYPES = frozenset(
    {"diagram_info", "chart_info", "section_header", "math", "figure_label"}
)
_MARKDOWN_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_BARE_URL_RE = re.compile(r"(?i)https?://[^\s<>\]\[()]+")
_DEFAULT_SESSION_DNS_BOUND: ContextVar[
    tuple[float, Callable[[], float]] | None
] = ContextVar("mathpix_default_session_dns_bound", default=None)
_MATHPIX_SUBMIT_FAILURE_CATEGORIES = frozenset(
    {
        "boundary_error",
        "cancelled",
        "deadline",
        "dns_error",
        "internal_error",
        "invalid_response",
        "missing_pdf_id",
        "redirect_response",
        "response_cap",
        "server_response",
        "session_entry",
        "session_exit",
        "transport_error",
    }
)
_MATHPIX_SESSION_INIT_FAILURE_CATEGORIES = frozenset(
    {"client_session", "connector", "resolver", "resolver_fallback"}
)


def _exception_chain_label(exc: BaseException | None) -> str:
    """Class identities only (module.qualname); never message content."""
    if exc is None:
        return ""
    names: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(names) < 4:
        seen.add(id(current))
        cls = type(current)
        names.append(f"{cls.__module__}.{cls.__qualname__}")
        if current.__cause__ is not None:
            current = current.__cause__
        elif not current.__suppress_context__:
            current = current.__context__
        else:
            current = None
    return " exc=" + "<-".join(names)


def _log_mathpix_submit_failure(
    category: str, exc: BaseException | None = None
) -> None:
    if category not in _MATHPIX_SUBMIT_FAILURE_CATEGORIES:
        category = "internal_error"
    print(
        f"[notesbot.mathpix-error] stage=submit category={category}"
        + _exception_chain_label(exc)
    )


def _log_mathpix_session_init_failure(
    category: str, exc: BaseException | None = None
) -> None:
    if category not in _MATHPIX_SESSION_INIT_FAILURE_CATEGORIES:
        category = "client_session"
    print(
        f"[notesbot.mathpix-error] stage=session_init category={category}"
        + _exception_chain_label(exc)
    )


class _RecoverableMathpixError(Exception):
    pass


class _MathpixDeadline(_RecoverableMathpixError):
    pass


class _MathpixCap(_RecoverableMathpixError):
    pass


class _ResponseCap(_RecoverableMathpixError):
    def __init__(self, status: int):
        self.status = status


class _DefaultSessionLifecycleError(Exception):
    def __init__(self, phase: Literal["entry", "exit"]):
        self.phase = phase


class _SubmitPhase:
    """Tracks whether a submit may have reached Mathpix before an ID exists."""

    __slots__ = ("state",)

    def __init__(self) -> None:
        self.state: Literal["before_dispatch", "dispatch_possible", "id_recovered"] = (
            "before_dispatch"
        )

    def mark_dispatch_possible(self) -> None:
        self.state = "dispatch_possible"

    def mark_id_recovered(self) -> None:
        self.state = "id_recovered"

    @property
    def is_ambiguous(self) -> bool:
        return self.state == "dispatch_possible"


def _valid_box(value: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    if not all(math.isfinite(item) for item in value):
        raise ValueError("mathpix_invalid_geometry")
    x0, y0, x1, y1 = value
    if not (0.0 <= x0 < x1 <= 100.0 and 0.0 <= y0 < y1 <= 100.0):
        raise ValueError("mathpix_invalid_geometry")
    return value


class MathpixLine(FrozenModel):
    local_page: int = Field(gt=0)
    box: tuple[float, float, float, float]
    polygon: tuple[tuple[float, float], ...]
    line_type: str
    subtype: str = ""
    role: EvidenceRole
    text: str = ""
    text_display: str = ""
    confidence: float | None = None
    confidence_rate: float | None = None
    uncertain: bool = False

    _box_is_valid = field_validator("box")(_valid_box)

    @field_validator("line_type")
    @classmethod
    def _type_is_nonempty(cls, value: str) -> str:
        if not value:
            raise ValueError("mathpix_invalid_schema")
        return value

    @field_validator("polygon")
    @classmethod
    def _polygon_is_bounded(
        cls, value: tuple[tuple[float, float], ...]
    ) -> tuple[tuple[float, float], ...]:
        if len(value) > config.MATHPIX_MAX_POLYGON_POINTS_PER_LINE:
            raise ValueError("mathpix_polygon_cap")
        if any(
            not math.isfinite(coordinate) or not 0.0 <= coordinate <= 100.0
            for point in value
            for coordinate in point
        ):
            raise ValueError("mathpix_invalid_geometry")
        return value

    @field_validator("confidence", "confidence_rate")
    @classmethod
    def _confidence_is_bounded(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or not 0.0 <= value <= 1.0):
            raise ValueError("mathpix_invalid_confidence")
        return value


class MathpixPage(FrozenModel):
    local_page: int = Field(gt=0)
    page_width: int = Field(gt=0)
    page_height: int = Field(gt=0)
    lines: tuple[MathpixLine, ...]


class MathpixEvidence(FrozenModel):
    chunk_index: int
    chunk_sha256: str
    first_global_page: int = Field(gt=0)
    page_count: int = Field(gt=0)
    pages: tuple[MathpixPage, ...]
    mmd_page_count: int = Field(gt=0)

    @model_validator(mode="after")
    def _pages_match_provenance(self) -> "MathpixEvidence":
        if tuple(page.local_page for page in self.pages) != tuple(range(1, self.page_count + 1)):
            raise ValueError("mathpix_invalid_pages")
        if any(
            line.local_page != page.local_page
            for page in self.pages
            for line in page.lines
        ):
            raise ValueError("mathpix_line_page_mismatch")
        if self.mmd_page_count != self.page_count:
            raise ValueError("mathpix_invalid_mmd_pages")
        return self


class AsyncHttpResponse(Protocol):
    status: int
    headers: Mapping[str, str]
    content: AsyncIterable[bytes]


class AsyncHttpSession(Protocol):
    def request(
        self, method: str, url: str, **kwargs: Any
    ) -> AsyncContextManager[AsyncHttpResponse]: ...


@asynccontextmanager
async def _default_session() -> AsyncIterable[aiohttp.ClientSession]:
    bound = _DEFAULT_SESSION_DNS_BOUND.get()
    if bound is None:
        raise RuntimeError("mathpix_default_session_deadline_missing")
    deadline_at, clock = bound
    resolver: aiohttp.abc.AbstractResolver
    if config.MATHPIX_DNS_RESOLVER == "getaddrinfo":
        try:
            resolver = aiohttp.ThreadedResolver()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as fallback_error:
            _log_mathpix_session_init_failure("resolver", fallback_error)
            raise
    else:
        try:
            resolver = AsyncDnsResolver(deadline_at, monotonic=clock)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as resolver_error:
            _log_mathpix_session_init_failure("resolver_fallback", resolver_error)
            try:
                resolver = aiohttp.ThreadedResolver()
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as fallback_error:
                _log_mathpix_session_init_failure("resolver", fallback_error)
                raise
    try:
        connector = aiohttp.TCPConnector(
            resolver=resolver,
            use_dns_cache=False,
            ttl_dns_cache=0,
            force_close=True,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as connector_error:
        _log_mathpix_session_init_failure("connector", connector_error)
        await resolver.close()
        raise
    try:
        client_session = aiohttp.ClientSession(
            connector=connector,
            connector_owner=False,
            trust_env=False,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as session_error:
        _log_mathpix_session_init_failure("client_session", session_error)
        await asyncio.gather(connector.close(), resolver.close(), return_exceptions=True)
        raise
    try:
        async with client_session as session:
            yield session
    finally:
        results = await asyncio.gather(
            connector.close(),
            resolver.close(),
            return_exceptions=True,
        )
        if any(isinstance(result, BaseException) for result in results):
            raise RuntimeError("mathpix_default_session_close_failed") from None


@asynccontextmanager
async def _default_session_at_deadline(
    session_factory: Callable[[], AsyncContextManager[AsyncHttpSession]],
    deadline_at: float,
    clock: Callable[[], float],
) -> AsyncIterable[AsyncHttpSession]:
    token = _DEFAULT_SESSION_DNS_BOUND.set((deadline_at, clock))
    manager = session_factory()
    try:
        remaining = deadline_at - clock()
        if remaining <= 0.0:
            raise _DefaultSessionLifecycleError("entry")
        try:
            session = await asyncio.wait_for(
                manager.__aenter__(),
                timeout=remaining,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as entry_error:
            raise _DefaultSessionLifecycleError("entry") from entry_error
        if clock() >= deadline_at:
            try:
                await _exit_default_session(
                    manager,
                    (None, None, None),
                    deadline_at,
                    clock,
                )
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException:
                pass
            raise _DefaultSessionLifecycleError("entry")

        try:
            yield session
        except BaseException as body_error:
            try:
                suppressed = await _exit_default_session(
                    manager,
                    (type(body_error), body_error, body_error.__traceback__),
                    deadline_at,
                    clock,
                )
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exit_error:
                raise _DefaultSessionLifecycleError("exit") from exit_error
            if not suppressed:
                raise
        else:
            try:
                await _exit_default_session(
                    manager,
                    (None, None, None),
                    deadline_at,
                    clock,
                )
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exit_error:
                raise _DefaultSessionLifecycleError("exit") from exit_error
    finally:
        _DEFAULT_SESSION_DNS_BOUND.reset(token)


async def _exit_default_session(
    manager: AsyncContextManager[AsyncHttpSession],
    exc_info: tuple[
        type[BaseException] | None,
        BaseException | None,
        TracebackType | None,
    ],
    deadline_at: float,
    clock: Callable[[], float],
) -> bool | None:
    exit_task = asyncio.create_task(manager.__aexit__(*exc_info))
    await asyncio.sleep(0)
    if exit_task.done():
        result = exit_task.result()
        if clock() > deadline_at:
            raise TimeoutError
        return result
    remaining = deadline_at - clock()
    if remaining <= 0.0:
        # Give the native session/connector/resolver close chain a fixed number
        # of ready-loop turns; none of these closes owns a worker or timer.
        for _ in range(3):
            if exit_task.done():
                break
            await asyncio.sleep(0)
        if exit_task.done():
            exit_task.result()
            raise TimeoutError
        exit_task.cancel()
        await asyncio.gather(exit_task, return_exceptions=True)
        raise TimeoutError
    try:
        result = await asyncio.wait_for(exit_task, timeout=remaining)
    except TimeoutError:
        exit_task.cancel()
        await asyncio.gather(exit_task, return_exceptions=True)
        raise
    if clock() > deadline_at:
        raise TimeoutError
    return result


async def _read_capped(
    response: AsyncHttpResponse,
    limit: int,
    deadline_at: float,
    clock: Callable[[], float],
) -> bytes:
    """Read one response without retaining data beyond the fixed cap."""
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except (TypeError, ValueError):
            raise _MathpixCap from None
        if declared < 0 or declared > limit:
            raise _MathpixCap
    chunks: list[bytes] = []
    size = 0
    iterator = response.content.iter_chunked(min(64 * 1024, limit + 1))  # type: ignore[attr-defined]
    while True:
        if clock() >= deadline_at:
            raise _MathpixDeadline
        try:
            chunk = await anext(iterator)
        except StopAsyncIteration:
            break
        if clock() >= deadline_at:
            raise _MathpixDeadline
        if not isinstance(chunk, bytes):
            raise _MathpixCap
        size += len(chunk)
        if size > limit:
            raise _MathpixCap
        chunks.append(chunk)
    if clock() >= deadline_at:
        raise _MathpixDeadline
    return b"".join(chunks)


def _timeout(remaining: float) -> aiohttp.ClientTimeout:
    socket = min(float(config.MATHPIX_SOCKET_TIMEOUT_SECONDS), remaining)
    return aiohttp.ClientTimeout(
        total=remaining,
        connect=socket,
        sock_connect=socket,
        sock_read=socket,
    )


async def _request_bytes(
    session: AsyncHttpSession,
    budget: VisualRunBudget,
    *,
    method: str,
    url: str,
    deadline_at: float,
    headers: Mapping[str, str],
    data: object | None = None,
    limit: int | None = None,
    on_dispatch: Callable[[], None] | None = None,
) -> tuple[int, bytes]:
    remaining = deadline_at - budget.clock()
    if remaining <= 0.0:
        raise _MathpixDeadline
    byte_limit = config.MATHPIX_MAX_RESPONSE_BYTES if limit is None else limit

    async def operation() -> tuple[int, bytes]:
        kwargs: dict[str, object] = {
            "headers": headers,
            "timeout": _timeout(remaining),
            "allow_redirects": False,
        }
        if data is not None:
            kwargs["data"] = data
        request_context = session.request(method, url, **kwargs)
        if on_dispatch is not None:
            on_dispatch()
        async with request_context as response:
            try:
                body = await _read_capped(response, byte_limit, deadline_at, budget.clock)
            except _MathpixCap:
                raise _ResponseCap(response.status) from None
            return response.status, body

    try:
        return await asyncio.wait_for(operation(), timeout=remaining)
    except aiohttp.ServerTimeoutError:
        raise
    except TimeoutError:
        raise _MathpixDeadline from None


def _strict_json_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _RecoverableMathpixError
        value[key] = item
    return value


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        decoded = body.decode("utf-8")
        value = json.loads(decoded, object_pairs_hook=_strict_json_object_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, _RecoverableMathpixError):
        raise _RecoverableMathpixError from None
    if not isinstance(value, dict):
        raise _RecoverableMathpixError
    return value


def _safe_pdf_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    identifier = value.strip()
    if not identifier or len(identifier.encode("utf-8")) > 1_024:
        return None
    return identifier


def _request_deadline(budget: VisualRunBudget, work_deadline: float) -> float:
    return min(
        work_deadline,
        budget.clock() + float(config.MATHPIX_REQUEST_TIMEOUT_SECONDS),
    )


def _auth_headers() -> dict[str, str]:
    return {"app_id": config.MATHPIX_APP_ID, "app_key": config.MATHPIX_APP_KEY}


def _submit_form(chunk: PdfChunk) -> aiohttp.FormData:
    options = {
        "metadata": {"improve_mathpix": False},
        "include_smiles": True,
        "include_page_breaks": True,
        "preserve_section_numbering": True,
    }
    form = aiohttp.FormData()
    form.add_field(
        "file",
        chunk.pdf_bytes,
        filename="lecture-chunk.pdf",
        content_type="application/pdf",
    )
    form.add_field(
        "options_json",
        json.dumps(options, separators=(",", ":")),
        content_type="application/json",
    )
    return form


async def _submit(
    chunk: PdfChunk,
    budget: VisualRunBudget,
    session: AsyncHttpSession,
    work_deadline: float,
) -> str | None:
    budget.take_mathpix_submit(chunk.index)
    deadline = _request_deadline(budget, work_deadline)
    phase = _SubmitPhase()
    try:
        status, body = await _request_bytes(
            session,
            budget,
            method="POST",
            url=f"{_BASE_URL}/v3/pdf",
            deadline_at=deadline,
            headers=_auth_headers(),
            data=_submit_form(chunk),
            on_dispatch=phase.mark_dispatch_possible,
        )
    except aiohttp.ClientConnectorError:
        return None
    except dns.exception.DNSException as exc:
        # Name resolution failed, so no connection existed and the chunk
        # cannot have reached Mathpix.  Log the class for diagnosis, then
        # degrade to source-only Gemini exactly like a connect failure.
        _log_mathpix_submit_failure("dns_error", exc)
        return None
    except _ResponseCap as exc:
        if 400 <= exc.status < 500:
            return None
        _log_mathpix_submit_failure("response_cap", exc)
        raise VisualFidelityError("mathpix_submit_unconfirmed") from None
    except VisualFidelityError as exc:
        _log_mathpix_submit_failure("boundary_error", exc)
        raise VisualFidelityError("mathpix_submit_unconfirmed") from None
    except _MathpixDeadline as exc:
        _log_mathpix_submit_failure("deadline", exc)
        raise VisualFidelityError("mathpix_submit_unconfirmed") from None
    except _MathpixCap as exc:
        _log_mathpix_submit_failure("response_cap", exc)
        raise VisualFidelityError("mathpix_submit_unconfirmed") from None
    except (aiohttp.ClientError, OSError) as exc:
        _log_mathpix_submit_failure("transport_error", exc)
        raise VisualFidelityError("mathpix_submit_unconfirmed") from None
    except asyncio.CancelledError as exc:
        if phase.is_ambiguous:
            _log_mathpix_submit_failure("cancelled", exc)
            raise VisualFidelityError("mathpix_submit_unconfirmed") from None
        raise
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if phase.is_ambiguous:
            _log_mathpix_submit_failure("internal_error", exc)
            raise VisualFidelityError("mathpix_submit_unconfirmed") from None
        raise
    if 400 <= status < 500:
        return None
    if status < 200 or status >= 300:
        _log_mathpix_submit_failure(
            "redirect_response" if 300 <= status < 400 else "server_response"
        )
        raise VisualFidelityError("mathpix_submit_unconfirmed") from None
    try:
        payload = _json_object(body)
    except _RecoverableMathpixError:
        _log_mathpix_submit_failure("invalid_response")
        raise VisualFidelityError("mathpix_submit_unconfirmed") from None
    pdf_id = _safe_pdf_id(payload.get("pdf_id"))
    if pdf_id is None:
        _log_mathpix_submit_failure("missing_pdf_id")
        raise VisualFidelityError("mathpix_submit_unconfirmed") from None
    phase.mark_id_recovered()
    return pdf_id


def _sanitize_markup(value: object) -> str:
    if not isinstance(value, str):
        raise _RecoverableMathpixError
    without_destinations = _MARKDOWN_LINK_RE.sub(lambda match: match.group(1), value)
    return " ".join(_BARE_URL_RE.sub("", without_destinations).split())


def _finite_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _RecoverableMathpixError
    result = float(value)
    if not math.isfinite(result):
        raise _RecoverableMathpixError
    return result


def _dimension(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _RecoverableMathpixError
    return value


def _region_box(region: object, width: int, height: int) -> tuple[float, float, float, float] | None:
    if not isinstance(region, dict):
        return None
    try:
        x0 = _finite_number(region.get("top_left_x", region.get("left", region.get("x"))))
        y0 = _finite_number(region.get("top_left_y", region.get("top", region.get("y"))))
        if "width" in region and "height" in region:
            x1 = x0 + _finite_number(region["width"])
            y1 = y0 + _finite_number(region["height"])
        else:
            x1 = _finite_number(region.get("bottom_right_x", region.get("right")))
            y1 = _finite_number(region.get("bottom_right_y", region.get("bottom")))
    except _RecoverableMathpixError:
        return None
    if not (0.0 <= x0 < x1 <= width and 0.0 <= y0 < y1 <= height):
        raise _RecoverableMathpixError
    return (100.0 * x0 / width, 100.0 * y0 / height, 100.0 * x1 / width, 100.0 * y1 / height)


def _polygon(
    value: object, width: int, height: int
) -> tuple[tuple[float, float], ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) < 3:
        return None
    if len(value) > config.MATHPIX_MAX_POLYGON_POINTS_PER_LINE:
        raise _MathpixCap
    points: list[tuple[float, float]] = []
    for raw in value:
        try:
            if isinstance(raw, dict):
                x, y = _finite_number(raw.get("x")), _finite_number(raw.get("y"))
            elif isinstance(raw, (list, tuple)) and len(raw) == 2:
                x, y = _finite_number(raw[0]), _finite_number(raw[1])
            else:
                return None
        except _RecoverableMathpixError:
            return None
        if not (0.0 <= x <= width and 0.0 <= y <= height):
            raise _RecoverableMathpixError
        points.append((100.0 * x / width, 100.0 * y / height))
    return tuple(points)


def _polygon_box(points: tuple[tuple[float, float], ...]) -> tuple[float, float, float, float]:
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    box = (min(xs), min(ys), max(xs), max(ys))
    return _valid_box(box)


def _intersects_expanded(
    box: tuple[float, float, float, float],
    visual: tuple[float, float, float, float],
) -> bool:
    vx0, vy0, vx1, vy1 = visual
    expanded = (max(0.0, vx0 - 20.0), max(0.0, vy0 - 20.0), min(100.0, vx1 + 20.0), min(100.0, vy1 + 20.0))
    x0, y0, x1, y1 = box
    return x0 < expanded[2] and x1 > expanded[0] and y0 < expanded[3] and y1 > expanded[1]


def _optional_confidence(value: object) -> float | None:
    if value is None:
        return None
    number = _finite_number(value)
    if not 0.0 <= number <= 1.0:
        raise _RecoverableMathpixError
    return number


def _mmd_pages(mmd: str, expected: int) -> tuple[str, ...]:
    pages = tuple(re.split(r"\\pagebreak\b", mmd))
    if len(pages) != expected:
        raise _RecoverableMathpixError
    return pages


def _parse_lines(body: bytes, mmd_body: bytes, chunk: PdfChunk) -> MathpixEvidence:
    payload = _json_object(body)
    pages_raw = payload.get("pages")
    if not isinstance(pages_raw, list):
        raise _RecoverableMathpixError
    try:
        mmd = mmd_body.decode("utf-8")
    except UnicodeDecodeError:
        raise _RecoverableMathpixError from None
    if len(mmd) > config.MATHPIX_MAX_MMD_CHARS:
        raise _MathpixCap
    mmd_pages = _mmd_pages(mmd, chunk.page_count)

    numbered: dict[int, tuple[int, int, list[object]]] = {}
    for raw_page in pages_raw:
        if not isinstance(raw_page, dict):
            raise _RecoverableMathpixError
        page_number = raw_page.get("page", raw_page.get("page_number"))
        if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number in numbered:
            raise _RecoverableMathpixError
        width = _dimension(raw_page.get("page_width", raw_page.get("width")))
        height = _dimension(raw_page.get("page_height", raw_page.get("height")))
        raw_lines = raw_page.get("lines")
        if not isinstance(raw_lines, list):
            raise _RecoverableMathpixError
        numbered[page_number] = (width, height, raw_lines)
    if set(numbered) != set(range(1, chunk.page_count + 1)):
        raise _RecoverableMathpixError

    pages: list[MathpixPage] = []
    total_text = 0
    total_lines = 0
    for page_number in range(1, chunk.page_count + 1):
        width, height, raw_lines = numbered[page_number]
        ordered = sorted(
            enumerate(raw_lines),
            key=lambda pair: (
                pair[1].get("line", pair[0]) if isinstance(pair[1], dict) and isinstance(pair[1].get("line", pair[0]), int) else pair[0],
                str(pair[1].get("id", "")) if isinstance(pair[1], dict) else "",
            ),
        )
        prepared: list[dict[str, Any]] = []
        for source_order, raw in ordered:
            if not isinstance(raw, dict):
                raise _RecoverableMathpixError
            line_type_raw = raw.get("type")
            if not isinstance(line_type_raw, str) or not line_type_raw.strip():
                raise _RecoverableMathpixError
            line_type = line_type_raw.strip().lower()
            subtype = _sanitize_markup(raw.get("subtype", ""))
            text = _sanitize_markup(raw.get("text", ""))
            text_display = _sanitize_markup(raw.get("text_display", ""))
            polygon = _polygon(raw.get("cnt"), width, height)
            region = _region_box(raw.get("region"), width, height)
            if polygon is not None:
                box = _polygon_box(polygon)
            elif region is not None:
                box = region
                polygon = ()
            else:
                raise _RecoverableMathpixError
            confidence = _optional_confidence(raw.get("confidence"))
            confidence_rate = _optional_confidence(raw.get("confidence_rate"))
            prepared.append(
                {
                    "source_order": source_order,
                    "line_type": line_type,
                    "subtype": subtype,
                    "text": text,
                    "text_display": text_display,
                    "polygon": polygon,
                    "box": box,
                    "confidence": confidence,
                    "confidence_rate": confidence_rate,
                }
            )
        visual_boxes = [record["box"] for record in prepared if record["line_type"] in _VISUAL_TYPES]
        retained: list[MathpixLine] = []
        for record in prepared:
            line_type = record["line_type"]
            if line_type in _VISUAL_TYPES:
                role: EvidenceRole = "visual_candidate"
            elif line_type in _LABEL_TYPES:
                role = "label_candidate"
            elif line_type in _CONTEXT_TYPES:
                if not any(_intersects_expanded(record["box"], visual) for visual in visual_boxes):
                    continue
                role = "context_candidate"
            elif record["polygon"]:
                role = "uncertain"
            else:
                continue
            uncertain = role == "uncertain" or any(
                value is not None and value < 0.75
                for value in (record["confidence"], record["confidence_rate"])
            )
            corroboration = record["text_display"] or record["text"]
            if line_type in _MMD_CORROBORATED_TYPES and corroboration and corroboration not in mmd_pages[page_number - 1]:
                uncertain = True
            line = MathpixLine(
                local_page=page_number,
                box=record["box"],
                polygon=record["polygon"],
                line_type=line_type,
                subtype=record["subtype"],
                role=role,
                text=record["text"],
                text_display=record["text_display"],
                confidence=record["confidence"],
                confidence_rate=record["confidence_rate"],
                uncertain=uncertain,
            )
            total_lines += 1
            total_text += len(line.text) + len(line.text_display)
            retained.append(line)
        if total_lines > config.MATHPIX_MAX_EVIDENCE_LINES or total_text > config.MATHPIX_MAX_EVIDENCE_TEXT_CHARS:
            raise _MathpixCap
        pages.append(MathpixPage(local_page=page_number, page_width=width, page_height=height, lines=tuple(retained)))
    return MathpixEvidence(
        chunk_index=chunk.index,
        chunk_sha256=chunk.sha256_hex,
        first_global_page=chunk.first_global_page,
        page_count=chunk.page_count,
        pages=tuple(pages),
        mmd_page_count=len(mmd_pages),
    )


async def _poll_until_complete(
    chunk: PdfChunk,
    budget: VisualRunBudget,
    session: AsyncHttpSession,
    pdf_id: str,
    work_deadline: float,
    sleeper: Callable[[float], Awaitable[None]],
) -> bool:
    encoded = quote(pdf_id, safe="")
    for attempt in range(config.MATHPIX_MAX_POLL_ATTEMPTS):
        deadline = _request_deadline(budget, work_deadline)
        if deadline <= budget.clock():
            budget.check_deadline()
            return False
        budget.take_mathpix_poll(chunk.index, config.MATHPIX_MAX_POLL_ATTEMPTS)
        status, body = await _request_bytes(
            session,
            budget,
            method="GET",
            url=f"{_BASE_URL}/v3/pdf/{encoded}",
            deadline_at=deadline,
            headers=_auth_headers(),
        )
        if not 200 <= status < 300:
            return False
        payload = _json_object(body)
        response_id = payload.get("pdf_id")
        if response_id is not None and response_id != pdf_id:
            return False
        state = payload.get("status")
        if state not in _POLL_STATES:
            return False
        if state == "completed":
            return True
        if state == "error":
            return False
        if attempt + 1 >= config.MATHPIX_MAX_POLL_ATTEMPTS:
            return False
        interval = float(config.MATHPIX_POLL_INTERVAL_SECONDS)
        if budget.clock() + interval > work_deadline:
            return False
        await sleeper(interval)
    return False


async def _download_evidence(
    chunk: PdfChunk,
    budget: VisualRunBudget,
    session: AsyncHttpSession,
    pdf_id: str,
    work_deadline: float,
) -> MathpixEvidence:
    encoded = quote(pdf_id, safe="")
    budget.take_mathpix_lines(chunk.index)
    lines_status, lines_body = await _request_bytes(
        session,
        budget,
        method="GET",
        url=f"{_BASE_URL}/v3/pdf/{encoded}.lines.json",
        deadline_at=_request_deadline(budget, work_deadline),
        headers=_auth_headers(),
    )
    if not 200 <= lines_status < 300:
        raise _RecoverableMathpixError
    budget.take_mathpix_mmd(chunk.index)
    mmd_status, mmd_body = await _request_bytes(
        session,
        budget,
        method="GET",
        url=f"{_BASE_URL}/v3/pdf/{encoded}.mmd",
        deadline_at=_request_deadline(budget, work_deadline),
        headers=_auth_headers(),
    )
    if not 200 <= mmd_status < 300:
        raise _RecoverableMathpixError
    try:
        return _parse_lines(lines_body, mmd_body, chunk)
    except _RecoverableMathpixError:
        raise
    except (TypeError, ValueError):
        raise _RecoverableMathpixError from None


async def _delete(
    chunk: PdfChunk,
    budget: VisualRunBudget,
    session: AsyncHttpSession,
    pdf_id: str,
    cleanup_deadline: float,
) -> None:
    try:
        budget.take_mathpix_delete(chunk.index)
        encoded = quote(pdf_id, safe="")
        status, body = await _request_bytes(
            session,
            budget,
            method="DELETE",
            url=f"{_BASE_URL}/v3/pdf/{encoded}",
            deadline_at=cleanup_deadline,
            headers=_auth_headers(),
        )
        if status in (404, 410):
            # The document no longer exists server-side, which is exactly the
            # privacy outcome cleanup must confirm.
            return
        if not 200 <= status < 300:
            raise _RecoverableMathpixError
        payload = _json_object(body)
        if payload.get("pdf_id") != pdf_id or payload.get("status") not in _POLL_STATES:
            raise _RecoverableMathpixError
    except VisualFidelityError:
        raise
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        print(
            "[notesbot.mathpix-error] stage=cleanup category=delete_failed"
            + _exception_chain_label(exc)
        )
        raise VisualFidelityError("mathpix_cleanup_failed") from None


async def _collect_mathpix_evidence_async(
    chunk: PdfChunk,
    budget: VisualRunBudget,
    *,
    session_factory: Callable[[], AsyncContextManager[AsyncHttpSession]],
    sleeper: Callable[[float], Awaitable[None]],
    separate_cleanup_session: bool = False,
) -> MathpixEvidence | None:
    work_deadline = budget.deadline_at - float(config.MATHPIX_CLEANUP_RESERVE_SECONDS)
    pdf_id: str | None = None

    async def collect_with_session(
        session: AsyncHttpSession,
    ) -> MathpixEvidence | None:
        nonlocal pdf_id
        pdf_id = await _submit(chunk, budget, session, work_deadline)
        if pdf_id is None:
            return None
        try:
            completed = await _poll_until_complete(
                chunk, budget, session, pdf_id, work_deadline, sleeper
            )
            if not completed:
                return None
            try:
                evidence = await _download_evidence(
                    chunk, budget, session, pdf_id, work_deadline
                )
                serialize_mathpix_evidence(evidence, chunk)
            except ValueError as exc:
                if str(exc) in {"mathpix_evidence_cap", "mathpix_evidence_url"}:
                    raise _RecoverableMathpixError from None
                raise
            return evidence
        except _MathpixDeadline:
            budget.check_deadline()
            return None
        except (
            _RecoverableMathpixError,
            aiohttp.ClientError,
            OSError,
            dns.exception.DNSException,
        ):
            return None

    if separate_cleanup_session:
        try:
            try:
                async with _default_session_at_deadline(
                    session_factory,
                    work_deadline,
                    budget.clock,
                ) as session:
                    return await collect_with_session(session)
            except _DefaultSessionLifecycleError as exc:
                code = (
                    "mathpix_cleanup_failed"
                    if pdf_id is not None
                    else "mathpix_submit_unconfirmed"
                )
                if pdf_id is None:
                    _log_mathpix_submit_failure(f"session_{exc.phase}", exc)
                raise VisualFidelityError(code) from None
        finally:
            if pdf_id is not None:
                cleanup_deadline = budget.clock() + float(
                    config.MATHPIX_CLEANUP_TIMEOUT_SECONDS
                )
                try:
                    async with _default_session_at_deadline(
                        session_factory,
                        cleanup_deadline,
                        budget.clock,
                    ) as cleanup_session:
                        await _delete(
                            chunk,
                            budget,
                            cleanup_session,
                            pdf_id,
                            cleanup_deadline,
                        )
                except VisualFidelityError:
                    raise
                except BaseException as exc:
                    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                        raise
                    raise VisualFidelityError("mathpix_cleanup_failed") from None

    async with session_factory() as session:
        try:
            return await collect_with_session(session)
        finally:
            if pdf_id is not None:
                cleanup_deadline = budget.clock() + float(
                    config.MATHPIX_CLEANUP_TIMEOUT_SECONDS
                )
                await _delete(
                    chunk,
                    budget,
                    session,
                    pdf_id,
                    cleanup_deadline,
                )


def collect_mathpix_evidence(
    chunk: PdfChunk,
    budget: VisualRunBudget,
    *,
    session_factory: Callable[[], AsyncContextManager[AsyncHttpSession]] | None = None,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> MathpixEvidence | None:
    """Return bounded source-derived proposals, or None for source-only Gemini."""
    if not config.MATHPIX_ENABLED or not config.MATHPIX_APP_ID or not config.MATHPIX_APP_KEY:
        return None
    budget.check_deadline()
    if budget.deadline_at - budget.clock() < config.MATHPIX_CLEANUP_RESERVE_SECONDS + 1:
        return None
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("mathpix_event_loop_active")
    owns_default_session = session_factory is None
    factory = session_factory or _default_session
    runner = asyncio.Runner()
    try:
        return runner.run(
            _collect_mathpix_evidence_async(
                chunk,
                budget,
                session_factory=factory,
                sleeper=sleeper,
                separate_cleanup_session=owns_default_session,
            )
        )
    finally:
        runner.close()


def serialize_mathpix_evidence(
    evidence: MathpixEvidence,
    chunk: PdfChunk,
) -> bytes:
    """Validate exact chunk provenance and return capped model-visible JSON."""
    if (
        evidence.chunk_index != chunk.index
        or evidence.chunk_sha256 != chunk.sha256_hex
        or evidence.first_global_page != chunk.first_global_page
        or evidence.page_count != chunk.page_count
        or evidence.mmd_page_count != chunk.page_count
        or tuple(page.local_page for page in evidence.pages)
        != tuple(range(1, chunk.page_count + 1))
        or any(
            line.local_page != page.local_page
            for page in evidence.pages
            for line in page.lines
        )
    ):
        raise ValueError("mathpix_evidence_provenance_invalid")
    pages: list[dict[str, object]] = []
    for page in evidence.pages:
        lines = [
            {
                "box": list(line.box),
                "confidence": line.confidence,
                "confidence_rate": line.confidence_rate,
                "local_page": line.local_page,
                "polygon": [list(point) for point in line.polygon],
                "role": line.role,
                "subtype": line.subtype,
                "text": line.text,
                "text_display": line.text_display,
                "type": line.line_type,
                "uncertain": line.uncertain,
            }
            for line in page.lines
        ]
        pages.append(
            {
                "lines": lines,
                "local_page": page.local_page,
                "page_height": page.page_height,
                "page_width": page.page_width,
            }
        )
    raw = json.dumps({"pages": pages}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    encoded_length = 4 * math.ceil(len(raw) / 3)
    if len(raw) > config.MATHPIX_MAX_EVIDENCE_JSON_BYTES or encoded_length > config.MATHPIX_MAX_EVIDENCE_ENCODED_BYTES:
        raise ValueError("mathpix_evidence_cap")
    if b"http://" in raw.lower() or b"https://" in raw.lower():
        raise ValueError("mathpix_evidence_url")
    return raw
