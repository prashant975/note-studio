"""Password-protected Vercel HTTP boundary for direct PDF generation."""
from __future__ import annotations

import hmac
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from typing import Callable, Mapping
from urllib.parse import parse_qsl, urlsplit

# Make the top-level package importable when Vercel loads this file from /api.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from notesbot.blob_store import BlobConflictError, BlobStore  # noqa: E402
from notesbot.web_auth import (  # noqa: E402
    SESSION_TTL_SECONDS,
    Session,
    issue_session,
    renew_session,
    require_same_origin,
    verify_session,
)
from notesbot.web_jobs import JobManifest  # noqa: E402
from notesbot.web_service import (  # noqa: E402
    DispatchUnavailable,
    InternalRunDispatcher,
    JobNotFound,
    JobNotReady,
    PublicJob,
    StartResult,
    BlobJobRepository,
    WebService,
    derive_owner,
    parse_start_request,
    parse_import_start_request,
    verify_run_token,
)
from notesbot.remote_pdf import RemotePdfImportError  # noqa: E402


MAX_JSON_BODY = 32 * 1024
MAX_CONTENT_LENGTH_DIGITS = 20
SESSION_COOKIE = "shortnotes_session"
RUN_TOKEN_HEADER = "X-Shortnotes-Run-Token"
CSRF_HEADER = "X-CSRF-Token"
SESSION_RENEWAL_WINDOW_SECONDS = 15 * 60

_SECURITY_HEADERS = (
    ("Content-Type", "application/json; charset=utf-8"),
    ("Cache-Control", "no-store"),
    (
        "Content-Security-Policy",
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    ),
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
    ("X-Robots-Tag", "noindex, nofollow"),
)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    payload: dict
    headers: tuple[tuple[str, str], ...] = ()


class _HttpError(Exception):
    def __init__(self, status: int, public_message: str) -> None:
        super().__init__(public_message)
        self.status = status
        self.public_message = public_message


class SlidingWindowLimiter:
    """Small bounded warm-instance limiter used only as defense in depth."""

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        *,
        max_keys: int = 1_024,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit <= 0
            or not isinstance(window_seconds, (int, float))
            or isinstance(window_seconds, bool)
            or window_seconds <= 0
            or isinstance(max_keys, bool)
            or not isinstance(max_keys, int)
            or max_keys <= 0
        ):
            raise ValueError("Invalid limiter configuration")
        self.limit = limit
        self.window_seconds = float(window_seconds)
        self.max_keys = max_keys
        self.clock = clock
        self._events: dict[str, list[float]] = {}
        self._last_seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def allow(self, key: object) -> bool:
        normalized = str(key)[:256] if key else "unknown"
        now = float(self.clock())
        cutoff = now - self.window_seconds
        with self._lock:
            events = [stamp for stamp in self._events.get(normalized, ()) if stamp > cutoff]
            if len(events) >= self.limit:
                self._events[normalized] = events
                self._last_seen[normalized] = now
                return False
            if normalized not in self._events and len(self._events) >= self.max_keys:
                oldest = min(self._last_seen, key=self._last_seen.get)
                self._events.pop(oldest, None)
                self._last_seen.pop(oldest, None)
            events.append(now)
            self._events[normalized] = events
            self._last_seen[normalized] = now
            return True


def _secure_response(
    status: int,
    payload: dict,
    extra_headers: tuple[tuple[str, str], ...] = (),
) -> HttpResponse:
    return HttpResponse(status, payload, _SECURITY_HEADERS + extra_headers)


def _header(headers: Mapping[str, str], name: str) -> str:
    matches = [
        value
        for key, value in headers.items()
        if isinstance(key, str) and key.lower() == name.lower()
    ]
    if len(matches) != 1 or not isinstance(matches[0], str):
        return ""
    value = matches[0]
    if "\x00" in value or "\r" in value or "\n" in value:
        return ""
    return value


def _constant_equal(left: object, right: object) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _parse_cookie(headers: Mapping[str, str]) -> str | None:
    raw = _header(headers, "Cookie")
    if not raw or len(raw) > 4_096:
        return None
    found: list[str] = []
    for item in raw.split(";"):
        part = item.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        if name.strip() == SESSION_COOKIE:
            if value != value.strip() or not value or len(value) > 512:
                return None
            found.append(value)
    return found[0] if len(found) == 1 else None


def _json_body(headers: Mapping[str, str], body: bytes) -> dict:
    content_type = _header(headers, "Content-Type")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise _HttpError(415, "JSON content type required")
    if not body:
        raise _HttpError(400, "Invalid request")

    def strict_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(
            body.decode("utf-8"),
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            object_pairs_hook=strict_object,
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError, RecursionError):
        raise _HttpError(400, "Invalid request") from None
    if not isinstance(value, dict):
        raise _HttpError(400, "Invalid request")
    return value


def _query(path: str) -> tuple[str, dict[str, str]]:
    try:
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment or parsed.path != "/api/web":
            raise _HttpError(404, "Not found")
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except _HttpError:
        raise
    except (TypeError, ValueError):
        raise _HttpError(400, "Invalid request") from None
    query: dict[str, str] = {}
    for key, value in pairs:
        if not key or key in query:
            raise _HttpError(400, "Invalid request")
        query[key] = value
    action = query.get("action", "")
    if not action:
        raise _HttpError(400, "Invalid request")
    return action, query


def _public_payload(value: object) -> dict:
    if isinstance(value, PublicJob):
        return value.to_dict()
    if isinstance(value, JobManifest):
        # Internal dispatch needs only a bounded acknowledgement. Filename and
        # settings are intentionally not re-read or exposed here.
        return {"jobId": value.id, "state": value.state, "error": value.error}
    raise RuntimeError("Invalid service response")


class WebApplication:
    """Pure route/application layer used by both Vercel and unit tests."""

    _METHODS = {
        "health": "GET",
        "session": "GET",
        "status": "GET",
        "auth": "POST",
        "start": "POST",
        "import": "POST",
        "run": "POST",
        "downloaded": "POST",
        "logout": "POST",
    }

    def __init__(
        self,
        service: WebService,
        *,
        password: str,
        session_secret: str,
        production_host: str = "",
        clock: Callable[[], int] = lambda: int(time.time()),
        login_limiter: SlidingWindowLimiter | None = None,
        start_limiter: SlidingWindowLimiter | None = None,
        import_limiter: SlidingWindowLimiter | None = None,
    ) -> None:
        if not isinstance(password, str) or not password:
            raise ValueError("Application password is required")
        if not isinstance(session_secret, str) or not session_secret:
            raise ValueError("Session secret is required")
        self.service = service
        self.password = password
        self.session_secret = session_secret
        self.production_host = production_host
        self.clock = clock
        self.login_limiter = login_limiter or SlidingWindowLimiter(5, 60, clock=clock)
        self.start_limiter = start_limiter or SlidingWindowLimiter(3, 60, clock=clock)
        self.import_limiter = import_limiter or SlidingWindowLimiter(3, 60, clock=clock)

    def handle(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
        client_key: str = "",
    ) -> HttpResponse:
        try:
            if not isinstance(body, bytes):
                raise _HttpError(400, "Invalid request")
            if len(body) > MAX_JSON_BODY:
                raise _HttpError(413, "Request too large")
            action, query = _query(path)
            if action not in self._METHODS:
                raise _HttpError(404, "Not found")
            if not isinstance(method, str) or method.upper() != self._METHODS[action]:
                raise _HttpError(405, "Method not allowed")
            method = method.upper()
            allowed_query = {"action", "jobId"} if action == "status" else {"action"}
            if set(query) != allowed_query:
                raise _HttpError(400, "Invalid request")
            if method == "GET" and body:
                raise _HttpError(400, "Invalid request")
            return self._route(action, query, headers, body, client_key)
        except _HttpError as error:
            return _secure_response(
                error.status, {"ok": False, "error": error.public_message}
            )
        except JobNotFound:
            return _secure_response(404, {"ok": False, "error": "Job not found"})
        except JobNotReady:
            return _secure_response(409, {"ok": False, "error": "Job is not ready"})
        except BlobConflictError:
            return _secure_response(409, {"ok": False, "error": "Job state changed"})
        except DispatchUnavailable:
            return _secure_response(
                503, {"ok": False, "error": "Generation service unavailable"}
            )
        except RemotePdfImportError as error:
            status = {"invalid_link":400,"private_destination":400,"not_public":422,"not_pdf":422,"too_large":413,"deadline_exceeded":408,"unavailable":503}[error.code]
            return _secure_response(status, {"ok": False, "error": str(error)})
        except ValueError:
            return _secure_response(400, {"ok": False, "error": "Invalid request"})
        except Exception:
            return _secure_response(
                503, {"ok": False, "error": "Service temporarily unavailable"}
            )

    def _same_origin(self, headers: Mapping[str, str]) -> bool:
        return require_same_origin(
            _header(headers, "Origin"),
            _header(headers, "Host"),
            self.production_host,
        )

    def _session(
        self,
        headers: Mapping[str, str],
        *,
        now: int | None = None,
    ) -> Session | None:
        token = _parse_cookie(headers)
        if token is None:
            return None
        current = int(self.clock()) if now is None else now
        return verify_session(token, self.session_secret, now=current)

    @staticmethod
    def _session_cookie(token: str, max_age: int = SESSION_TTL_SECONDS) -> str:
        return (
            f"{SESSION_COOKIE}={token}; Max-Age={max_age}; Path=/; "
            "HttpOnly; Secure; SameSite=Strict"
        )

    def _renewal_headers(
        self,
        session: Session,
        *,
        now: int,
    ) -> tuple[tuple[str, str], ...]:
        if session.expires_at - now > SESSION_RENEWAL_WINDOW_SECONDS:
            return ()
        token = renew_session(self.session_secret, session, now=now)
        return (("Set-Cookie", self._session_cookie(token)),)

    def _browser_authority(
        self,
        headers: Mapping[str, str],
        *,
        state_change: bool,
    ) -> tuple[Session, str, int]:
        now = int(self.clock())
        session = self._session(headers, now=now)
        if session is None:
            raise _HttpError(401, "Unauthorized")
        if state_change:
            if not self._same_origin(headers):
                raise _HttpError(403, "Request rejected")
            if not _constant_equal(_header(headers, CSRF_HEADER), session.csrf):
                raise _HttpError(403, "Request rejected")
        return session, derive_owner(self.session_secret, session.nonce), now

    def _route(
        self,
        action: str,
        query: dict[str, str],
        headers: Mapping[str, str],
        body: bytes,
        client_key: str,
    ) -> HttpResponse:
        if action == "health":
            return _secure_response(200, {"ok": True})

        if action == "auth":
            if not self.login_limiter.allow(client_key):
                raise _HttpError(429, "Too many requests")
            if not self._same_origin(headers):
                raise _HttpError(403, "Request rejected")
            value = _json_body(headers, body)
            if set(value) != {"password"} or not isinstance(value["password"], str) or len(value["password"]) > 256:
                raise _HttpError(400, "Invalid request")
            if not _constant_equal(value["password"], self.password):
                raise _HttpError(401, "Unauthorized")
            token, csrf = issue_session(self.session_secret, now=int(self.clock()))
            cookie = self._session_cookie(token)
            return _secure_response(200, {"csrf": csrf}, (("Set-Cookie", cookie),))

        if action == "session":
            now = int(self.clock())
            session = self._session(headers, now=now)
            if session is None:
                return _secure_response(200, {"authenticated": False})
            return _secure_response(
                200,
                {"authenticated": True, "csrf": session.csrf},
                self._renewal_headers(session, now=now),
            )

        if action == "status":
            session, owner, now = self._browser_authority(
                headers, state_change=False
            )
            job_id = query["jobId"]
            public = self.service.status(owner, job_id)
            return _secure_response(
                200,
                _public_payload(public),
                self._renewal_headers(session, now=now),
            )

        if action == "run":
            value = _json_body(headers, body)
            if set(value) != {"owner", "jobId"}:
                raise _HttpError(400, "Invalid request")
            owner = value["owner"]
            job_id = value["jobId"]
            token = _header(headers, RUN_TOKEN_HEADER)
            if not verify_run_token(
                token,
                self.session_secret,
                owner,
                job_id,
                now=int(self.clock()),
            ):
                raise _HttpError(401, "Unauthorized")
            result = self.service.run(owner, job_id)
            return _secure_response(200, _public_payload(result))

        session, owner, now = self._browser_authority(
            headers, state_change=True
        )
        value = _json_body(headers, body)

        if action == "start":
            request = parse_start_request(
                value, owner=owner, store_host=self.service.store_host
            )
            if not self.start_limiter.allow(owner):
                raise _HttpError(429, "Too many requests")
            result = self.service.start(owner, request)
            if not isinstance(result, StartResult):
                raise RuntimeError("Invalid service response")
            payload = result.job.to_dict()
            payload["created"] = result.created
            return _secure_response(
                202 if result.created else 200,
                payload,
                self._renewal_headers(session, now=now),
            )

        if action == "import":
            request = parse_import_start_request(value)
            if not self.start_limiter.allow(owner) or not self.import_limiter.allow(owner):
                raise _HttpError(429, "Too many requests")
            result = self.service.import_and_start(owner, request)
            if not isinstance(result, StartResult): raise RuntimeError("Invalid service response")
            payload = result.job.to_dict(); payload["created"] = result.created
            return _secure_response(202, payload, self._renewal_headers(session, now=now))

        if action == "downloaded":
            if set(value) != {"jobId", "fetched"} or value.get("fetched") is not True:
                raise _HttpError(400, "Invalid request")
            result = self.service.downloaded(owner, value.get("jobId"), value["fetched"])
            return _secure_response(200, {"ok": True, "deleted": result.deleted})

        if action == "logout":
            if value != {}:
                raise _HttpError(400, "Invalid request")
            cookie = (
                f"{SESSION_COOKIE}=; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT; "
                "Path=/; HttpOnly; Secure; SameSite=Strict"
            )
            return _secure_response(200, {"ok": True}, (("Set-Cookie", cookie),))

        raise _HttpError(404, "Not found")


def _trusted_origin() -> str:
    explicit = os.getenv("APP_ORIGIN", "").strip()
    if explicit:
        return explicit
    host = (
        os.getenv("VERCEL_PROJECT_PRODUCTION_URL", "").strip()
        or os.getenv("VERCEL_URL", "").strip()
    )
    if not host or any(character in host for character in "/:@?#\\"):
        raise RuntimeError("Application origin is unavailable")
    return f"https://{host}"


def _lazy_generate(*args, **kwargs):
    from notesbot.gemini_client import generate_short_notes

    return generate_short_notes(*args, **kwargs)


def _lazy_render(*args, **kwargs):
    from notesbot.notes_pdf import build_notes_pdf

    return build_notes_pdf(*args, **kwargs)


def _build_application() -> WebApplication:
    password = os.getenv("APP_PASSWORD", "")
    session_secret = os.getenv("APP_SESSION_SECRET", "")
    store_id = os.getenv("BLOB_STORE_ID", "")
    if not password or not session_secret or not store_id:
        raise RuntimeError("Web application is not configured")
    origin = _trusted_origin()
    store = BlobStore(store_id)
    service = WebService(
        BlobJobRepository(store),
        store,
        InternalRunDispatcher(origin),
        _lazy_generate,
        _lazy_render,
        run_secret=session_secret,
    )
    return WebApplication(
        service,
        password=password,
        session_secret=session_secret,
        # Browser mutations are protected by an exact HTTPS Origin == Host
        # check.  Do not pin that check to one Vercel alias: production,
        # team, and custom aliases can all legitimately serve this deployment.
        production_host="",
    )


_APP: WebApplication | None = None


def _get_application() -> WebApplication:
    global _APP
    if _APP is None:
        _APP = _build_application()
    return _APP


def _register_oidc_headers(headers: Mapping[str, str]) -> None:
    """Register this invocation before a later Blob operation requests OIDC."""
    try:
        from vercel.oidc import set_headers
    except ImportError:
        # Task 8 installs the runtime dependency. Ordinary auth/route tests do
        # not need it; an actual Blob call still fails closed in BlobStore.
        return
    set_headers(dict(headers))


def _collect_headers(message) -> dict[str, str]:
    collected: dict[str, str] = {}
    for key, value in message.raw_items():
        existing_key = next(
            (candidate for candidate in collected if candidate.lower() == key.lower()),
            None,
        )
        if existing_key is None:
            collected[key] = value
        else:
            # Sensitive duplicates become invalid at the pure boundary.
            collected[existing_key] = collected[existing_key] + "\x00" + value
    return collected


class handler(BaseHTTPRequestHandler):
    """Thin Vercel adapter; all policy lives in ``WebApplication``."""

    def send_error(self, code, message=None, explain=None) -> None:
        """Keep BaseHTTPRequestHandler-generated failures inside our boundary."""
        del message, explain
        status = code if isinstance(code, int) and not isinstance(code, bool) else 500
        if status < 400 or status > 599:
            status = 500
        public_message = "Invalid request"
        if status == 501:
            status = 405
            public_message = "Method not allowed"
        self._send(
            _secure_response(status, {"ok": False, "error": public_message}),
            write_body=getattr(self, "command", "") != "HEAD",
        )

    def _send(self, response: HttpResponse, *, write_body: bool = True) -> None:
        data = json.dumps(
            response.payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        self.send_response(response.status)
        for key, value in response.headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if write_body:
            self.wfile.write(data)

    def _serve(self, *, write_body: bool = True) -> None:
        headers = _collect_headers(self.headers)
        transfer_encoding = _header(headers, "Transfer-Encoding")
        raw_lengths = self.headers.get_all("Content-Length") or []
        if transfer_encoding or len(raw_lengths) > 1:
            self._send(_secure_response(400, {"ok": False, "error": "Invalid request"}), write_body=write_body)
            return
        length = 0
        if raw_lengths:
            raw_length = raw_lengths[0]
            if (
                len(raw_length) > MAX_CONTENT_LENGTH_DIGITS
                or not raw_length.isascii()
                or not raw_length.isdigit()
            ):
                self._send(_secure_response(400, {"ok": False, "error": "Invalid request"}), write_body=write_body)
                return
            length = int(raw_length)
        if length > MAX_JSON_BODY:
            self._send(_secure_response(413, {"ok": False, "error": "Request too large"}), write_body=write_body)
            return
        if self.command == "GET" and length:
            self._send(_secure_response(400, {"ok": False, "error": "Invalid request"}), write_body=write_body)
            return
        body = self.rfile.read(length) if length else b""
        if len(body) != length:
            self._send(_secure_response(400, {"ok": False, "error": "Invalid request"}), write_body=write_body)
            return
        try:
            _register_oidc_headers(headers)
            response = _get_application().handle(
                self.command,
                self.path,
                headers,
                body,
                client_key=self.client_address[0] if self.client_address else "",
            )
        except Exception:
            response = _secure_response(
                503, {"ok": False, "error": "Service temporarily unavailable"}
            )
        self._send(response, write_body=write_body)

    def do_GET(self) -> None:  # noqa: N802
        self._serve()

    def do_POST(self) -> None:  # noqa: N802
        self._serve()

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(write_body=False)

    def do_PUT(self) -> None:  # noqa: N802
        self._serve()

    def do_PATCH(self) -> None:  # noqa: N802
        self._serve()

    def do_DELETE(self) -> None:  # noqa: N802
        self._serve()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._serve()
