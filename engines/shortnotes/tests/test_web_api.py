"""Transport-independent tests for the Vercel web API boundary."""
from __future__ import annotations

from email.message import Message
import io
import json

import pytest

import api.web as web_api
from api.web import HttpResponse, SlidingWindowLimiter, WebApplication
from notesbot.web_auth import SESSION_TTL_SECONDS, issue_session, verify_session
from notesbot.web_service import (
    DispatchUnavailable,
    DownloadResult,
    PublicJob,
    StartResult,
    derive_owner,
    issue_run_token,
)


SECRET = "session-secret"
PASSWORD = "academics"
HOST = "shortnotes-azure.vercel.app"
ORIGIN = f"https://{HOST}"
COOKIE_NAME = "shortnotes_session"
JOB_ID = "a" * 32


class FakeService:
    def __init__(self):
        self.store_host = "4mlmgdlkyhhksimu.private.blob.vercel-storage.com"
        self.calls = []
        self.failure = None

    def _raise(self):
        if self.failure:
            raise self.failure

    def start(self, owner, request):
        self.calls.append(("start", owner, request))
        self._raise()
        return StartResult(
            job=PublicJob(request.id, "queued", request.filename, 1_000, 1_000, ""),
            created=True,
        )

    def import_and_start(self, owner, request):
        self.calls.append(("import", owner, request))
        self._raise()
        return StartResult(
            job=PublicJob(request.id, "queued", "linked-lecture.pdf", 1_000, 1_000, ""),
            created=True,
        )

    def status(self, owner, job_id):
        self.calls.append(("status", owner, job_id))
        self._raise()
        return PublicJob(job_id, "running", "lecture.pdf", 1_000, 1_001, "")

    def run(self, owner, job_id):
        self.calls.append(("run", owner, job_id))
        self._raise()
        return PublicJob(job_id, "complete", "lecture.pdf", 1_000, 1_010, "")

    def downloaded(self, owner, job_id, fetched):
        self.calls.append(("downloaded", owner, job_id, fetched))
        self._raise()
        return DownloadResult(deleted=True)


def app(service=None, *, clock=lambda: 1_000, login_limit=5, start_limit=3, import_limit=3):
    return WebApplication(
        service or FakeService(),
        password=PASSWORD,
        session_secret=SECRET,
        production_host=HOST,
        clock=clock,
        login_limiter=SlidingWindowLimiter(login_limit, 60, clock=clock),
        start_limiter=SlidingWindowLimiter(start_limit, 60, clock=clock),
        import_limiter=SlidingWindowLimiter(import_limit, 60, clock=clock),
    )


def headers(*, cookie="", csrf="", origin=ORIGIN, host=HOST, run_token=""):
    value = {
        "Host": host,
        "Origin": origin,
        "Content-Type": "application/json",
    }
    if cookie:
        value["Cookie"] = f"{COOKIE_NAME}={cookie}"
    if csrf:
        value["X-CSRF-Token"] = csrf
    if run_token:
        value["X-Shortnotes-Run-Token"] = run_token
    return value


def post(application, action, payload, request_headers):
    return application.handle(
        "POST",
        f"/api/web?action={action}",
        request_headers,
        json.dumps(payload).encode(),
        client_key="203.0.113.8",
    )


def login(application):
    response = post(application, "auth", {"password": PASSWORD}, headers())
    cookie_header = dict(response.headers)["Set-Cookie"]
    token = cookie_header.split(";", 1)[0].split("=", 1)[1]
    return response, token, response.payload["csrf"]


def assert_security_headers(response: HttpResponse):
    values = {key.lower(): value for key, value in response.headers}
    assert values["cache-control"] == "no-store"
    assert values["content-type"] == "application/json; charset=utf-8"
    assert values["content-security-policy"].startswith("default-src 'none'")
    assert values["x-frame-options"] == "DENY"
    assert values["x-content-type-options"] == "nosniff"
    assert values["referrer-policy"] == "no-referrer"


def valid_start():
    owner = derive_owner(SECRET, verify_session(issue_session(SECRET, now=1_000)[0], SECRET, now=1_000).nonce)
    source = (
        "https://4mlmgdlkyhhksimu.private.blob.vercel-storage.com/"
        f"shortnotes/v1/inputs/{owner}/{JOB_ID}.pdf"
    )
    return {
        "requestId": JOB_ID,
        "inputUrl": source,
        "inputSize": 4_096,
        "filename": "lecture.pdf",
        "teacherName": "Teacher",
        "subject": "chemistry",
        "language": "hinglish",
        "instructions": "",
        "watermark": True,
        "logoWatermark": False,
    }


def valid_import():
    value = valid_start()
    value.pop("inputUrl"); value.pop("inputSize"); value.pop("filename")
    value["pdfUrl"] = "https://public.example/lecture.pdf?ephemeral=1"
    return value


def test_import_requires_session_same_origin_csrf_exact_keys_and_owner_rate_limit():
    service = FakeService(); application = app(service, import_limit=1)
    _, token, csrf = login(application)
    for request_headers in (headers(csrf=csrf), headers(cookie=token, csrf=csrf, origin="https://evil.example"), headers(cookie=token, csrf="wrong")):
        assert post(application, "import", valid_import(), request_headers).status in {401, 403}
    malformed = valid_import(); malformed["unexpected"] = True
    assert post(application, "import", malformed, headers(cookie=token, csrf=csrf)).status == 400
    accepted = post(application, "import", valid_import(), headers(cookie=token, csrf=csrf))
    assert accepted.status == 202 and accepted.payload == {
        "jobId": JOB_ID, "state": "queued", "filename": "linked-lecture.pdf",
        "createdAt": 1_000, "updatedAt": 1_000, "error": "", "created": True,
    }
    assert post(application, "import", valid_import(), headers(cookie=token, csrf=csrf)).status == 429
    assert [call[0] for call in service.calls] == ["import"]


def test_import_response_matches_normal_public_job_payload_and_exposes_no_private_input_metadata():
    service = FakeService(); application = app(service); _, token, csrf = login(application)
    response = post(application, "import", valid_import(), headers(cookie=token, csrf=csrf))
    assert response.status == 202
    assert set(response.payload) == {"jobId", "state", "filename", "createdAt", "updatedAt", "error", "created"}
    assert "pdfUrl" not in response.payload and "inputUrl" not in response.payload


def test_health_and_every_error_receive_no_store_security_headers():
    application = app()
    responses = [
        application.handle("GET", "/api/web?action=health", {}, b""),
        application.handle("GET", "/api/web?action=unknown", {}, b""),
        application.handle("POST", "/api/web?action=auth", headers(), b"not-json"),
    ]
    assert responses[0].status == 200
    assert responses[0].payload == {"ok": True}
    assert responses[1].status in {404, 405}
    assert responses[2].status == 400
    for response in responses:
        assert_security_headers(response)


def test_auth_uses_exact_secure_cookie_and_returns_only_csrf():
    application = app()
    response, token, csrf = login(application)
    assert response.status == 200
    assert response.payload == {"csrf": csrf}
    cookie = dict(response.headers)["Set-Cookie"]
    assert cookie.startswith(f"{COOKIE_NAME}={token}; Max-Age=43200; Path=/;")
    assert "; HttpOnly;" in cookie
    assert "; Secure;" in cookie
    assert cookie.endswith("SameSite=Strict")
    assert verify_session(token, SECRET, now=1_000).csrf == csrf


def test_auth_accepts_an_alternate_vercel_alias_when_origin_exactly_matches_host():
    application = WebApplication(
        FakeService(),
        password=PASSWORD,
        session_secret=SECRET,
        production_host="",
        clock=lambda: 1_000,
    )
    alias = "shortnotes-pw01-projects.vercel.app"
    response = post(
        application,
        "auth",
        {"password": PASSWORD},
        headers(origin=f"https://{alias}", host=alias),
    )
    assert response.status == 200


def test_auth_rejects_wrong_password_cross_origin_and_is_bounded_per_client():
    application = app(login_limit=2)
    wrong = post(application, "auth", {"password": "wrong"}, headers())
    cross = post(
        application, "auth", {"password": PASSWORD},
        headers(origin="https://evil.example"),
    )
    limited = post(application, "auth", {"password": PASSWORD}, headers())
    assert (wrong.status, cross.status, limited.status) == (401, 403, 429)
    assert "wrong" not in repr(wrong.payload)


def test_session_refresh_returns_csrf_but_never_nonce_owner_or_cookie_token():
    application = app()
    _, token, csrf = login(application)
    response = application.handle(
        "GET", "/api/web?action=session", {"Cookie": f"{COOKIE_NAME}={token}"}, b"",
    )
    assert response.payload == {"authenticated": True, "csrf": csrf}
    serialized = json.dumps(response.payload)
    assert token not in serialized
    assert verify_session(token, SECRET, now=1_000).nonce not in serialized
    assert derive_owner(SECRET, verify_session(token, SECRET, now=1_000).nonce) not in serialized

    duplicate_cookie = application.handle(
        "GET", "/api/web?action=session",
        {"Cookie": f"{COOKIE_NAME}={token}; {COOKIE_NAME}={token}"}, b"",
    )
    assert duplicate_cookie.payload == {"authenticated": False}


def test_start_near_expiry_renews_same_owner_and_csrf_for_job_recovery():
    now = [1_000]
    service = FakeService()
    application = app(service, clock=lambda: now[0])
    _, token, csrf = login(application)
    original = verify_session(token, SECRET, now=now[0])
    owner = derive_owner(SECRET, original.nonce)
    payload = valid_start()
    payload["inputUrl"] = (
        f"https://{service.store_host}/shortnotes/v1/inputs/{owner}/{JOB_ID}.pdf"
    )
    now[0] = original.expires_at - 12 * 60

    response = post(application, "start", payload, headers(cookie=token, csrf=csrf))

    renewed_cookie = dict(response.headers)["Set-Cookie"]
    renewed_token = renewed_cookie.split(";", 1)[0].split("=", 1)[1]
    renewed = verify_session(renewed_token, SECRET, now=now[0])
    assert response.status == 202
    assert renewed is not None
    assert renewed.csrf == csrf
    assert renewed.nonce == original.nonce
    assert renewed.expires_at == now[0] + SESSION_TTL_SECONDS
    assert service.calls[0][0:2] == ("start", owner)


def test_session_check_near_expiry_refreshes_before_a_new_upload():
    now = [1_000]
    application = app(clock=lambda: now[0])
    _, token, csrf = login(application)
    original = verify_session(token, SECRET, now=now[0])
    now[0] = original.expires_at - 12 * 60

    response = application.handle(
        "GET", "/api/web?action=session", {"Cookie": f"{COOKIE_NAME}={token}"}, b"",
    )

    renewed_token = dict(response.headers)["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
    renewed = verify_session(renewed_token, SECRET, now=now[0])
    assert response.payload == {"authenticated": True, "csrf": csrf}
    assert renewed is not None
    assert renewed.csrf == csrf
    assert renewed.nonce == original.nonce


def test_non_ascii_session_mac_is_a_clean_unauthorized_response():
    application = app()
    _, token, _csrf = login(application)
    malformed = ".".join([*token.split(".")[:4], "\u00e9" * 43])

    response = application.handle(
        "GET",
        f"/api/web?action=status&jobId={JOB_ID}",
        {"Cookie": f"{COOKIE_NAME}={malformed}"},
        b"",
    )

    assert response.status == 401
    assert response.payload == {"ok": False, "error": "Unauthorized"}


def test_start_requires_session_exact_origin_and_matching_csrf_before_service_call():
    service = FakeService()
    application = app(service)
    _, token, csrf = login(application)
    payload = valid_start()
    # Rebuild the source for this independently-issued session.
    session = verify_session(token, SECRET, now=1_000)
    owner = derive_owner(SECRET, session.nonce)
    payload["inputUrl"] = (
        f"https://{service.store_host}/shortnotes/v1/inputs/{owner}/{JOB_ID}.pdf"
    )

    for request_headers in [
        headers(csrf=csrf),
        headers(cookie=token, csrf=csrf, origin="https://evil.example"),
        headers(cookie=token, csrf="b" * 32),
    ]:
        response = post(application, "start", payload, request_headers)
        assert response.status in {401, 403}
    assert service.calls == []

    response = post(application, "start", payload, headers(cookie=token, csrf=csrf))
    assert response.status == 202
    assert response.payload["jobId"] == JOB_ID
    assert len(service.calls) == 1
    assert service.calls[0][0:2] == ("start", owner)


@pytest.mark.parametrize(
    "change",
    [
        {"requestId": "A" * 32},
        {"inputSize": 100 * 1024 * 1024 + 1},
        {"teacherName": " "},
        {"subject": "history"},
        {"language": "french"},
        {"instructions": "x" * 1_001},
        {"watermark": 1},
        {"logoWatermark": 1},
        {"extra": True},
    ],
)
def test_start_rejects_strict_invalid_payload_without_service_side_effect(change):
    service = FakeService()
    application = app(service)
    _, token, csrf = login(application)
    session = verify_session(token, SECRET, now=1_000)
    owner = derive_owner(SECRET, session.nonce)
    payload = valid_start()
    payload["inputUrl"] = f"https://{service.store_host}/shortnotes/v1/inputs/{owner}/{JOB_ID}.pdf"
    payload.update(change)

    response = post(application, "start", payload, headers(cookie=token, csrf=csrf))
    assert response.status == 400
    assert service.calls == []


def test_internal_run_requires_bound_hmac_and_browser_session_cannot_substitute():
    service = FakeService()
    application = app(service)
    _, cookie, csrf = login(application)
    owner = "b" * 32
    payload = {"owner": owner, "jobId": JOB_ID}

    browser_only = post(
        application, "run", payload, headers(cookie=cookie, csrf=csrf),
    )
    assert browser_only.status == 401
    assert service.calls == []

    token = issue_run_token(SECRET, owner, JOB_ID, now=1_000)
    accepted = post(application, "run", payload, headers(run_token=token))
    assert accepted.status == 200
    assert service.calls == [("run", owner, JOB_ID)]
    assert "owner" not in accepted.payload
    assert "url" not in json.dumps(accepted.payload).lower()


def test_status_and_download_receipt_are_owned_redacted_and_ordered_by_authority():
    service = FakeService()
    application = app(service)
    _, token, csrf = login(application)
    session = verify_session(token, SECRET, now=1_000)
    owner = derive_owner(SECRET, session.nonce)

    status = application.handle(
        "GET", f"/api/web?action=status&jobId={JOB_ID}",
        {"Cookie": f"{COOKIE_NAME}={token}"}, b"",
    )
    assert status.status == 200
    assert status.payload == {
        "jobId": JOB_ID,
        "state": "running",
        "filename": "lecture.pdf",
        "createdAt": 1_000,
        "updatedAt": 1_001,
        "error": "",
    }
    assert service.calls[-1] == ("status", owner, JOB_ID)

    rejected = post(
        application, "downloaded", {"jobId": JOB_ID, "fetched": False},
        headers(cookie=token, csrf=csrf),
    )
    assert rejected.status == 400
    assert service.calls[-1][0] == "status"

    receipt = post(
        application, "downloaded", {"jobId": JOB_ID, "fetched": True},
        headers(cookie=token, csrf=csrf),
    )
    assert receipt.status == 200
    assert receipt.payload == {"ok": True, "deleted": True}
    assert service.calls[-1] == ("downloaded", owner, JOB_ID, True)


def test_logout_requires_csrf_and_expires_the_cookie():
    application = app()
    _, token, csrf = login(application)
    rejected = post(application, "logout", {}, headers(cookie=token, csrf="b" * 32))
    assert rejected.status == 403
    accepted = post(application, "logout", {}, headers(cookie=token, csrf=csrf))
    assert accepted.status == 200
    cookie = dict(accepted.headers)["Set-Cookie"]
    assert cookie.startswith(f"{COOKIE_NAME}=; Max-Age=0;")
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=Strict" in cookie


@pytest.mark.parametrize(
    ("method", "path", "body", "expected"),
    [
        ("PUT", "/api/web?action=health", b"", 405),
        ("POST", "/api/web?action=health", b"{}", 405),
        ("GET", "/api/web?action=auth", b"", 405),
        ("GET", "/api/web?action=health&action=status", b"", 400),
        ("GET", "/wrong?action=health", b"", 404),
        ("GET", "/api/web?action=health&extra=1", b"", 400),
        ("POST", "/api/web?action=auth", b"{", 400),
        ("POST", "/api/web?action=auth", b"{}" + b" " * (32 * 1024), 413),
    ],
    ids=[
        "unsupported-method",
        "post-get-route",
        "get-post-route",
        "duplicate-action",
        "wrong-path",
        "unexpected-query",
        "malformed-json",
        "oversized-json",
    ],
)
def test_method_query_and_json_body_matrix_is_bounded(method, path, body, expected):
    response = app().handle(method, path, headers(), body, client_key="test")
    assert response.status == expected
    assert_security_headers(response)


def test_exceptions_are_mapped_without_provider_prompt_pdf_or_stack_detail():
    service = FakeService()
    service.failure = RuntimeError(
        "provider=https://private.blob token=super-secret PDF=lecture prompt=hidden"
    )
    application = app(service)
    _, token, _csrf = login(application)
    response = application.handle(
        "GET", f"/api/web?action=status&jobId={JOB_ID}",
        {"Cookie": f"{COOKIE_NAME}={token}"}, b"",
    )
    assert response.status == 503
    serialized = json.dumps(response.payload).lower()
    for forbidden in ("provider", "private.blob", "super-secret", "lecture", "prompt"):
        assert forbidden not in serialized


def test_duplicate_json_keys_are_rejected_instead_of_using_the_last_password():
    response = app().handle(
        "POST",
        "/api/web?action=auth",
        headers(),
        b'{"password":"wrong","password":"academics"}',
        client_key="duplicate-json",
    )
    assert response.status == 400
    assert "Set-Cookie" not in dict(response.headers)


def test_deeply_nested_json_is_a_bounded_bad_request_not_a_service_failure():
    body = ('{"value":' * 1_100 + "0" + "}" * 1_100).encode("ascii")
    assert len(body) < web_api.MAX_JSON_BODY

    response = app().handle(
        "POST", "/api/web?action=auth", headers(), body, client_key="deep-json",
    )

    assert response.status == 400
    assert response.payload == {"ok": False, "error": "Invalid request"}
    assert_security_headers(response)


def test_start_limiter_is_bounded_and_does_not_call_service_after_limit():
    now = [1_000]
    service = FakeService()
    application = app(service, clock=lambda: now[0], start_limit=1)
    _, token, csrf = login(application)
    session = verify_session(token, SECRET, now=1_000)
    owner = derive_owner(SECRET, session.nonce)
    payload = valid_start()
    payload["inputUrl"] = f"https://{service.store_host}/shortnotes/v1/inputs/{owner}/{JOB_ID}.pdf"

    assert post(application, "start", payload, headers(cookie=token, csrf=csrf)).status == 202
    assert post(application, "start", payload, headers(cookie=token, csrf=csrf)).status == 429
    assert len(service.calls) == 1
    now[0] = 1_061
    assert post(application, "start", payload, headers(cookie=token, csrf=csrf)).status == 202
    assert len(service.calls) == 2


def test_vercel_adapter_registers_incoming_headers_before_application_blob_work(monkeypatch):
    events = []

    class FakeApplication:
        def handle(self, method, path, request_headers, body, client_key=""):
            events.append(("handle", request_headers["X-Vercel-OIDC-Token"]))
            return HttpResponse(200, {"ok": True})

    monkeypatch.setattr(
        web_api,
        "_register_oidc_headers",
        lambda request_headers: events.append(
            ("register", request_headers["X-Vercel-OIDC-Token"])
        ),
    )
    monkeypatch.setattr(web_api, "_get_application", lambda: FakeApplication())

    endpoint = object.__new__(web_api.handler)
    message = Message()
    message["Host"] = HOST
    message["X-Vercel-OIDC-Token"] = "request-scoped-token"
    endpoint.headers = message
    endpoint.command = "GET"
    endpoint.path = "/api/web?action=health"
    endpoint.client_address = ("203.0.113.9", 443)
    endpoint.rfile = io.BytesIO()
    endpoint.wfile = io.BytesIO()
    endpoint.send_response = lambda _status: None
    endpoint.send_header = lambda _key, _value: None
    endpoint.end_headers = lambda: None

    endpoint._serve()

    assert events == [
        ("register", "request-scoped-token"),
        ("handle", "request-scoped-token"),
    ]


@pytest.mark.parametrize("raw_length", ["9" * 5_000, "0" * 5_000])
def test_vercel_adapter_rejects_unbounded_content_length_with_secure_json(
    monkeypatch, raw_length,
):
    monkeypatch.setattr(
        web_api,
        "_get_application",
        lambda: pytest.fail("an invalid length must be rejected before app construction"),
    )
    endpoint = object.__new__(web_api.handler)
    message = Message()
    message["Content-Length"] = raw_length
    endpoint.headers = message
    endpoint.command = "POST"
    endpoint.path = "/api/web?action=auth"
    endpoint.client_address = ("203.0.113.9", 443)
    endpoint.rfile = io.BytesIO()
    endpoint.wfile = io.BytesIO()
    sent = {"headers": []}
    endpoint.send_response = lambda status, *_args: sent.update(status=status)
    endpoint.send_header = lambda key, value: sent["headers"].append((key, value))
    endpoint.end_headers = lambda: None

    endpoint._serve()

    assert sent["status"] == 400
    response_headers = {key.lower(): value for key, value in sent["headers"]}
    assert response_headers["cache-control"] == "no-store"
    assert response_headers["content-type"] == "application/json; charset=utf-8"
    assert response_headers["x-content-type-options"] == "nosniff"
    assert json.loads(endpoint.wfile.getvalue()) == {
        "ok": False,
        "error": "Invalid request",
    }


def test_vercel_adapter_accepts_exact_body_cap_and_rejects_one_byte_more(monkeypatch):
    seen = []

    class FakeApplication:
        def handle(self, method, path, request_headers, body, client_key=""):
            seen.append((method, path, len(body)))
            return web_api._secure_response(200, {"ok": True})

    monkeypatch.setattr(web_api, "_get_application", lambda: FakeApplication())
    monkeypatch.setattr(web_api, "_register_oidc_headers", lambda _headers: None)

    def invoke(length, body):
        endpoint = object.__new__(web_api.handler)
        message = Message()
        message["Content-Length"] = str(length)
        endpoint.headers = message
        endpoint.command = "POST"
        endpoint.path = "/api/web?action=auth"
        endpoint.client_address = ("203.0.113.9", 443)
        endpoint.rfile = io.BytesIO(body)
        endpoint.wfile = io.BytesIO()
        sent = {"headers": []}
        endpoint.send_response = lambda status: sent.update(status=status)
        endpoint.send_header = lambda key, value: sent["headers"].append((key, value))
        endpoint.end_headers = lambda: None
        endpoint._serve()
        return sent

    accepted = invoke(web_api.MAX_JSON_BODY, b"x" * web_api.MAX_JSON_BODY)
    rejected = invoke(web_api.MAX_JSON_BODY + 1, b"")

    assert accepted["status"] == 200
    assert seen == [("POST", "/api/web?action=auth", web_api.MAX_JSON_BODY)]
    assert rejected["status"] == 413


def test_base_handler_errors_are_replaced_with_secure_redacted_json():
    endpoint = object.__new__(web_api.handler)
    endpoint.command = "TRACE"
    endpoint.wfile = io.BytesIO()
    sent = {"headers": []}
    endpoint.send_response = lambda status, *_args: sent.update(status=status)
    endpoint.send_header = lambda key, value: sent["headers"].append((key, value))
    endpoint.end_headers = lambda: None
    endpoint.log_error = lambda *_args: None

    endpoint.send_error(501, "provider token and stack detail")

    assert sent["status"] == 405
    response_headers = {key.lower(): value for key, value in sent["headers"]}
    assert response_headers["content-type"] == "application/json; charset=utf-8"
    assert response_headers["cache-control"] == "no-store"
    payload = json.loads(endpoint.wfile.getvalue())
    assert payload == {"ok": False, "error": "Method not allowed"}
    assert "provider" not in repr(payload).lower()
    assert "token" not in repr(payload).lower()
