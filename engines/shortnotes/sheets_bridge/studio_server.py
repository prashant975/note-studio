"""Serve the Short Notes Studio app locally *with* the automation button.

    python -m sheets_bridge --serve            # http://localhost:3000

Routes
  /                      the Studio UI (public/): Automation page
  /upload                the same UI on its Upload PDF page
  /api/web?action=...    the real password/session API from api/web.py
  /api/automation/...    the one-button automation (status/config/run/stop),
                         only for a logged-in Studio session
  /automation            optional details page (log, per-row table)
  /api/blob-*, /api/cleanup  -> 503: Vercel-only (Blob + OIDC), not local

The Studio's own generate/upload flow normally needs Vercel Blob; locally it
is backed by files on disk (``local_blob.LocalBlobStore``) and the job is run
in a background thread instead of a second HTTP hop, so the whole
upload -> generate -> download path works on this machine.
"""
from __future__ import annotations

import json
import mimetypes
import os
import secrets
import sys
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .auth_session import AutomationAuth
from .errors import BridgeError, describe_exception
from .local_blob import LocalBlobStore
from .webui import RunController, read_json_body, render_page, safe_api_response

REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DIR = REPO_ROOT / "public"
SESSION_COOKIE = "shortnotes_session"
AUTOMATION_PREFIX = "/api/automation/"
LOCAL_BLOB_PREFIX = "/api/local-blob/"
JOB_ID = __import__("re").compile(r"[0-9a-f]{32}\Z")


def _local_env_defaults() -> str:
    """The Studio API needs these to boot; fill sane local values for any that
    are missing/blank (a real .env value wins). Returns the password in use."""
    if not os.environ.get("APP_PASSWORD"):
        os.environ["APP_PASSWORD"] = "localdev"
    if not os.environ.get("APP_SESSION_SECRET"):
        os.environ["APP_SESSION_SECRET"] = secrets.token_hex(32)
    if not os.environ.get("BLOB_STORE_ID"):
        os.environ["BLOB_STORE_ID"] = "store_localdevstore01"
    origin = os.environ.get("APP_ORIGIN", "")
    if not origin.startswith("https://"):
        # InternalRunDispatcher requires https; it is never reached locally.
        os.environ["APP_ORIGIN"] = "https://localhost"
    return os.environ["APP_PASSWORD"]


def _cookie(header: str, name: str) -> str:
    for part in (header or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name and value:
            return value
    return ""


def serve_studio(settings, *, port: int = 3000, open_browser: bool = True) -> int:
    from .studio_instance import activate_existing_studio

    if activate_existing_studio(port, open_browser=open_browser):
        return 0
    password = _local_env_defaults()
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from api import web as web_module           # the real Vercel boundary (unchanged)
    from api.web import handler as WebHandler
    from notesbot.web_auth import verify_session
    from notesbot.web_service import derive_owner

    session_secret = os.environ["APP_SESSION_SECRET"]
    # The automation spends money with a particular person's Google access, so
    # it carries its own gate on top of the Studio password: sign in, and be on
    # the PW allowlist, before any /api/automation route answers.
    auth = AutomationAuth(settings)
    controller = RunController(settings, auth=auth)
    from . import output_cache

    swept = output_cache.sweep()
    if swept:
        print(f"  pending-notes cache: removed {swept} stale file(s)", flush=True)
    restored = auth.status()
    if restored.get("signedIn"):
        print(f"  automation: running as {restored.get('email')} (cached token)", flush=True)
    else:
        print("  automation: no cached Google token - run 'python -m sheets_bridge --check-setup' once", flush=True)

    # --- make the Studio's own flow work without Vercel ----------------------
    store = LocalBlobStore(os.environ["BLOB_STORE_ID"])

    class _LocalDispatcher:
        """Run the job here instead of POSTing back to a deployed origin."""

        def __init__(self, origin: str, *, session: object | None = None) -> None:
            self.origin = origin

        def __call__(self, owner: str, job_id: str, token: str) -> None:
            def work() -> None:
                # The job's own owner is the seat that uploaded it, so the
                # model call is billed to the person who asked for it.
                _current_owner.value = owner
                try:
                    web_module._get_application().service.run(owner, job_id)
                except Exception as exc:  # noqa: BLE001 - the job record carries the failure
                    # Class + location only: the redaction contract holds.
                    print(f"  manual job {job_id[:8]}: {describe_exception(exc)}", flush=True)

            threading.Thread(target=work, name=f"studio-job-{job_id[:8]}", daemon=True).start()

    web_module.BlobStore = lambda _store_id: store      # noqa: E731 - swap, not a redefinition
    web_module.InternalRunDispatcher = _LocalDispatcher

    # The manual flow needs a model too, and this app holds no provider keys:
    # route it through the PW proxy with the operator's Google sign-in, the
    # same way the automation does. One task_id per manual job.
    _current_owner = threading.local()

    def _ensure_proxy(owner: str = ""):
        """The requesting seat's proxy access.

        The manual flow spends money too, so it uses the Google account of the
        browser session that asked - never a cached token lying on the disk.
        The transport is process-wide, so it is re-installed for whoever is
        calling right now.
        """
        seat = owner or getattr(_current_owner, "value", "")
        account = auth.account(seat)
        if account is None:
            raise BridgeError("sign_in_required", "sign in with Google before generating")
        account.access.install_transport()
        return account.access

    def _signed_in(owner: str = "") -> bool:
        try:
            _ensure_proxy(owner)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _local_generate(pdf_bytes, edit_instruction="", language=None, subject_hint=""):
        from notesbot.gemini_client import generate_short_notes

        from .generator import page_count
        from .pw_gate import new_task_id
        from .pw_transport import set_task

        _ensure_proxy()
        set_task(
            task_id=new_task_id("studio-manual"),
            filename="studio-upload.pdf",
            input_unit="No. of pages",
            count=page_count(pdf_bytes) or None,
        )
        return generate_short_notes(pdf_bytes, edit_instruction, language, subject_hint)

    def _local_render(*args, **kwargs):
        from notesbot.notes_pdf import build_notes_pdf

        from . import pdf_branding, pdf_fonts

        pdf_fonts.install()      # Hindi lectures must render as Hindi
        pdf_branding.install()   # PW logo + PW watermark
        kwargs.setdefault("logo_watermark", True)
        return build_notes_pdf(*args, **kwargs)

    web_module._lazy_generate = _local_generate
    web_module._lazy_render = _local_render

    class Handler(WebHandler):
        server_version = "shortnotes-local/0.2"

        def log_message(self, fmt, *args):
            return

        # -- helpers ------------------------------------------------------------
        def _raw(self, status: int, body: bytes, content_type: str, write_body: bool = True) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if write_body:
                self.wfile.write(body)

        def _json(self, status: int, payload: dict, write_body: bool = True) -> None:
            self._raw(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", write_body)

        def _logged_in(self) -> bool:
            token = _cookie(self.headers.get("Cookie", ""), SESSION_COOKIE)
            if not token:
                return False
            return verify_session(token, session_secret, now=int(time.time())) is not None

        def _owner(self) -> str:
            """The caller's seat id: the same derivation the Blob endpoints use.

            No CSRF header is required here - that guards state changes and is
            checked separately on POST - so a plain GET can still say who is
            asking.
            """
            token = _cookie(self.headers.get("Cookie", ""), SESSION_COOKIE)
            if not token:
                return ""
            session = verify_session(token, session_secret, now=int(time.time()))
            if session is None:
                return ""
            return derive_owner(session_secret, session.nonce)

        def _same_origin_post(self) -> bool:
            if self.headers.get("X-Requested-With", "") != "sheets-bridge":
                return False
            origin = self.headers.get("Origin", "")
            host = self.headers.get("Host", "")
            if not origin:
                return False  # browsers always send Origin on POST
            parts = urlsplit(origin)
            return parts.scheme == "http" and parts.netloc == host and parts.hostname in ("localhost", "127.0.0.1")

        def _static(self, write_body: bool = True) -> bool:
            path = urlsplit(self.path).path
            if path == "/automation":
                self._raw(200, render_page(AUTOMATION_PREFIX.rstrip("/")), "text/html; charset=utf-8", write_body)
                return True
            if path.startswith("/api/"):
                return False
            if path in ("/", "/upload", "/upload/"):
                path = "/index.html"
            rel = os.path.normpath(path.lstrip("/"))
            full = (PUBLIC_DIR / rel).resolve()
            if PUBLIC_DIR.resolve() not in full.parents or not full.is_file():
                self._raw(404, b"Not found", "text/plain", write_body)
                return True
            ctype, _ = mimetypes.guess_type(str(full))
            if full.suffix == ".js":
                ctype = "text/javascript"
            self._raw(200, full.read_bytes(), ctype or "application/octet-stream", write_body)
            return True

        def _session_owner(self) -> str:
            """Owner id for the signed-in browser session (same derivation the
            Vercel endpoints use)."""
            token = _cookie(self.headers.get("Cookie", ""), SESSION_COOKIE)
            if not token:
                return ""
            session = verify_session(token, session_secret, now=int(time.time()))
            if session is None:
                return ""
            if self.headers.get("X-CSRF-Token", "") != session.csrf:
                return ""
            return derive_owner(session_secret, session.nonce)

        def _local_blob(self, method: str) -> bool:
            """Upload/download endpoints that stand in for Vercel Blob."""
            parsed = urlsplit(self.path)
            path = parsed.path
            query = dict(
                pair.split("=", 1) if "=" in pair else (pair, "")
                for pair in parsed.query.split("&") if pair
            )

            # 1) the prepare handshake the client already performs
            if path == "/api/blob-upload" and method == "POST" and parsed.query == "action=prepare":
                owner = self._session_owner()
                if not owner:
                    self._json(401, {"error": "Unauthorized"})
                    return True
                body = read_json_body(self) or {}
                request_id = str(body.get("requestId", ""))
                if not JOB_ID.match(request_id):
                    self._json(400, {"error": "Invalid request"})
                    return True
                if not _signed_in(owner):
                    self._json(503, {"error": (
                        "Google sign-in needed before generating: click Run automation once "
                        "(it signs in and checks the PW allowlist), then try again."
                    )})
                    return True
                self._json(200, {
                    "pathname": f"shortnotes/v1/inputs/{owner}/{request_id}.pdf",
                    "local": True,
                    "uploadUrl": f"{LOCAL_BLOB_PREFIX}upload?requestId={request_id}",
                })
                return True

            # 2) the upload itself (the client PUTs the file here in local mode)
            if path == LOCAL_BLOB_PREFIX + "upload" and method == "PUT":
                owner = self._session_owner()
                if not owner:
                    self._json(401, {"error": "Unauthorized"})
                    return True
                request_id = query.get("requestId", "")
                if not JOB_ID.match(request_id):
                    self._json(400, {"error": "Invalid request"})
                    return True
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if length <= 0 or length > 100 * 1024 * 1024:
                    self._json(413, {"error": "Invalid upload size"})
                    return True
                data = self.rfile.read(length)
                if len(data) != length or not data.startswith(b"%PDF"):
                    self._json(400, {"error": "Invalid PDF"})
                    return True
                pathname = f"shortnotes/v1/inputs/{owner}/{request_id}.pdf"
                try:
                    ref = store.put_bytes(pathname, data, "application/pdf", False)
                except Exception:  # noqa: BLE001 - existing input means a duplicate job
                    self._json(409, {"error": "This request was already uploaded"})
                    return True
                self._json(200, {"url": ref.url, "pathname": ref.pathname, "size": len(data)})
                return True

            # 3) the signed-download handshake -> a same-origin local URL
            if path == "/api/blob-download" and method == "POST":
                owner = self._session_owner()
                if not owner:
                    self._json(401, {"error": "Unauthorized"})
                    return True
                body = read_json_body(self) or {}
                job_id = str(body.get("jobId", ""))
                if not JOB_ID.match(job_id):
                    self._json(400, {"error": "Invalid request"})
                    return True
                try:
                    public = web_module._get_application().service.status(owner, job_id)
                except Exception:  # noqa: BLE001
                    self._json(404, {"error": "Job not found"})
                    return True
                payload = public.to_dict() if hasattr(public, "to_dict") else {}
                if payload.get("state") != "complete":
                    self._json(409, {"error": "Job is not ready"})
                    return True
                self._json(200, {
                    "url": f"http://{self.headers.get('Host', 'localhost:3000')}{LOCAL_BLOB_PREFIX}get?jobId={job_id}",
                    "filename": payload.get("filename") or "Concise Notes.pdf",
                })
                return True

            # 4) the actual bytes
            if path == LOCAL_BLOB_PREFIX + "get" and method == "GET":
                owner = ""
                token = _cookie(self.headers.get("Cookie", ""), SESSION_COOKIE)
                if token:
                    session = verify_session(token, session_secret, now=int(time.time()))
                    if session is not None:
                        owner = derive_owner(session_secret, session.nonce)
                job_id = query.get("jobId", "")
                if not owner or not JOB_ID.match(job_id):
                    self._json(401, {"error": "Unauthorized"})
                    return True
                from notesbot.web_service import owned_paths

                try:
                    public = web_module._get_application().service.status(owner, job_id)
                    payload = public.to_dict() if hasattr(public, "to_dict") else {}
                    if payload.get("state") != "complete":
                        self._json(409, {"error": "Job is not ready"})
                        return True
                    data = store.get_bytes(store.url_for(owned_paths(owner, job_id).output), 100 * 1024 * 1024)
                except Exception:  # noqa: BLE001
                    self._json(404, {"error": "Not found"})
                    return True
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Length", str(len(data)))
                from urllib.parse import quote
                filename = payload.get("filename") or "Concise Notes.pdf"
                self.send_header("Content-Disposition", 'attachment; filename="Concise Notes.pdf"; filename*=UTF-8\'\'' + quote(filename, safe=""))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return True

            return False

        def _manual_flow_stub(self) -> bool:
            """The Studio's own upload flow needs Vercel Blob, which does not
            exist locally. Answer definitively instead of letting the page
            retry a job that can never appear:

              status  -> 404 so a saved job id stops being resumed and the UI
                         offers "Abandon old job"
              start   -> 503 with the reason, pointing at Run automation
            """
            parsed = urlsplit(self.path)
            if parsed.path != "/api/web":
                return False
            action = ""
            for pair in parsed.query.split("&"):
                key, _, value = pair.partition("=")
                if key == "action":
                    action = value
            if action == "import":
                self._json(503, {
                    "ok": False,
                    "error": "Importing from a link needs the deployed app. Upload the PDF file instead, or use Run automation.",
                })
                return True
            return False

        def _blob_stub(self) -> bool:
            path = urlsplit(self.path).path
            if path in ("/api/cleanup",):
                self._json(503, {"ok": False, "error": "This needs Vercel Blob storage and is not available in the local app. Use the Run automation button here."})
                return True
            return False

        def _automation(self, method: str) -> bool:
            path = urlsplit(self.path).path
            if not path.startswith(AUTOMATION_PREFIX):
                return False
            route = path[len(AUTOMATION_PREFIX):]
            owner = self._owner()
            if not owner:
                self._json(401, {"ok": False, "error": "Unauthorized"})
                return True
            body: dict | None = None
            if method == "POST":
                if not self._same_origin_post():
                    self._json(403, {"ok": False, "error": "Request rejected"})
                    return True
                body = read_json_body(self)
                if body is None:
                    self._json(400, {"ok": False, "error": "invalid json"})
                    return True
            status, payload = safe_api_response(controller, method, route, body, owner)
            self._json(status, payload)
            return True

        # -- verbs ------------------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            if self._automation("GET") or self._local_blob("GET") or self._static() or self._blob_stub() or self._manual_flow_stub():
                return
            super().do_GET()

        def do_HEAD(self) -> None:  # noqa: N802
            if self._static(write_body=False) or self._blob_stub():
                return
            super().do_HEAD()

        def do_POST(self) -> None:  # noqa: N802
            if self._automation("POST") or self._local_blob("POST") or self._blob_stub() or self._manual_flow_stub():
                return
            super().do_POST()

        def do_PUT(self) -> None:  # noqa: N802
            if self._local_blob("PUT"):
                return
            super().do_PUT()

    class SingleInstanceServer(ThreadingHTTPServer):
        # Windows lets a second process bind the same address when
        # SO_REUSEADDR is set, so two servers end up sharing the port and
        # requests land on either one. Refuse instead: fail loudly.
        allow_reuse_address = False

    try:
        server = SingleInstanceServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        # Another launch may have bound the port after the initial check.
        if activate_existing_studio(port, open_browser=open_browser):
            return 0
        print(f"cannot start on port {port}: {exc.strerror or exc}.", flush=True)
        print("Another copy of the app is already running - close it (or use --port N).", flush=True)
        return 2
    url = f"http://localhost:{server.server_address[1]}/"
    print(f"Short Notes Studio (local) + automation: {url}", flush=True)
    print(f"  password: {password}   (APP_PASSWORD from .env, or 'localdev')", flush=True)
    print(f"  automation details page: {url}automation", flush=True)
    print(f"  local Blob store: {store.root}   (Ctrl+C to quit)", flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping ...", flush=True)
        controller.stop()
        # The run thread is a daemon: exiting now would kill it mid-row, with
        # the model call already paid for and the row left 'claimed'. Let the
        # current row finish; a second Ctrl+C abandons it.
        if controller.running():
            print("  finishing the current row before exit (Ctrl+C again to abandon it) ...", flush=True)
            try:
                while not controller.wait(5):
                    print("  ... still working", flush=True)
            except KeyboardInterrupt:
                print("  abandoned: that row stays 'claimed' and is retried after 3 h", flush=True)
    finally:
        server.server_close()
    return 0
