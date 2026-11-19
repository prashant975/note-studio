"""
pw_access.py - PW App Kit v2 client for Python backends/local apps.

Copy this file into an app, set APP_NAME, and route all paid provider calls
through the shared PW proxy. The app ships no provider keys. The proxy verifies
the signed-in PW user, checks the app whitelist, makes the provider call with
proxy-held keys, and writes trusted raw usage logs to the usage database
(apps never connect to it themselves).

Usage logging model:
  - One successful proxy/provider call is recorded by the proxy. You do not
    log anything yourself.
  - The proxy ROLLS UP those calls server-side into one row per
    (task, app, user, file, model) and the cost sheet reads the finished rows.
    Apps and dashboards no longer combine raw rows themselves.
  - ALWAYS pass a task_id. It is the only thing tying a job's calls together;
    without one the job's volume cannot be totalled at all.
  - count = what THIS call processed. See CONNECT-TO-PW-PROXY.md section 6.
  - filename = the user's input file (never a step name); input_unit = the
    tool's Unit in the Key Map; no new input -> leave count blank.
  - Do not use client-side combined logging for normal AI calls.

Everywhere a google_token is accepted you may pass either:
  - the token string from login, or
  - a zero-arg function returning a fresh Google token.

The kit automatically exchanges the Google token for a proxy-issued 7-day
session pass, so long runs do not fail just because Google's token expires.
"""

import base64
import copy
import hashlib
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

import requests


# ---------------------------------------------------------------------------
# Per-app config. APP_NAME must exactly match a header in Whitelisted row 1.
# ---------------------------------------------------------------------------
APP_NAME = "Handwritten Notes Automation"
# Which kit this file came from; UPDATE-YOUR-APP.md lists what changed. Keep
# it as shipped - the only line an app edits is APP_NAME.
KIT_VERSION = "2026-11-18"

PROXY_BASE_URL = os.environ.get(
    "PW_PROXY_BASE_URL", "https://pw-apps-proxy.vercel.app"
).rstrip("/")

_TIMEOUT = 30
_AI_TIMEOUT = 300
_LARGE_REQUEST_BYTES = 3_500_000


class PWAccessError(Exception):
    """Raised when a paid proxy/provider call fails."""


def new_task_id(prefix: str = "task") -> str:
    """Create a stable id for one user task/run."""
    safe = "".join(ch for ch in str(prefix or "task") if ch.isalnum() or ch in "-_")
    return f"{safe or 'task'}-{uuid.uuid4().hex}"


def _headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _resolve_token(google_token) -> str:
    return google_token() if callable(google_token) else google_token


# Session passes, keyed by the Google token that minted them.
#
# This was a single shared slot, which is correct for a desktop build — one
# process, one user — and wrong the moment the app runs as a server. There, the
# first user to sign in filled the slot and every later request reused THEIR
# pass, so the proxy saw one identity for everybody: usage was billed to the
# wrong person, and the app-wise allowlist answered for the cached user rather
# than the caller. Observed in production, Sep 2026.
_sessions: Dict[str, Dict[str, Any]] = {}
_sessions_lock = threading.Lock()
_MAX_SESSIONS = 500


def _session_key(google_token: str) -> str:
    return hashlib.sha256((google_token or "").encode("utf-8")).hexdigest()


def _auth_token(google_token, force_new: bool = False) -> str:
    """Return this user's cached 7-day proxy session pass, or mint one."""
    import time

    g = _resolve_token(google_token)
    key = _session_key(g)

    if not force_new:
        with _sessions_lock:
            hit = _sessions.get(key)
            if hit and time.time() < hit["expiry"] - 60:
                return hit["token"]

    try:
        r = requests.post(
            f"{PROXY_BASE_URL}/api/session",
            headers=_headers(g),
            json={},
            timeout=_TIMEOUT,
        )
        if r.status_code == 200:
            data = r.json()
            token = data.get("session_token") or ""
            if token:
                with _sessions_lock:
                    # Cheap bound: this is a cache, not a registry, and a
                    # dropped entry only costs one extra mint.
                    if len(_sessions) >= _MAX_SESSIONS:
                        _sessions.clear()
                    _sessions[key] = {
                        "token": token,
                        "expiry": float(data.get("expires_at_ms") or 0) / 1000.0,
                    }
                return token
    except Exception:
        pass
    return g


def _post(path: str, google_token, payload: Dict[str, Any], timeout: int):
    """POST to the proxy. On 401, mint a fresh proxy session pass once."""
    r = requests.post(
        f"{PROXY_BASE_URL}{path}",
        headers=_headers(_auth_token(google_token)),
        json=payload,
        timeout=timeout,
    )
    if r.status_code == 401:
        r = requests.post(
            f"{PROXY_BASE_URL}{path}",
            headers=_headers(_auth_token(google_token, force_new=True)),
            json=payload,
            timeout=timeout,
        )
    return r


def check_allowed(google_token, app: str = APP_NAME) -> bool:
    """Call before every paid/main action. Fail closed."""
    return check_allowed_status(google_token, app) == "allowed"


def check_allowed_status(google_token, app: str = APP_NAME) -> str:
    """Return one of: allowed, denied, error."""
    if not google_token:
        return "denied"
    try:
        r = _post("/api/allowlist", google_token, {"app": app}, _TIMEOUT)
        if r.status_code == 200:
            return "allowed" if bool(r.json().get("allowed")) else "denied"
        if r.status_code == 403:
            return "denied"
        return "error"
    except Exception:
        return "error"


def log_usage(
    google_token,
    *,
    filename: str,
    input_unit: str,
    count: Any,
    items: List[Dict[str, Any]],
    task_id: str = "",
    video_duration: str = "",
    app: str = APP_NAME,
) -> Optional[dict]:
    """Legacy/manual audit endpoint.

    Do not use this for normal AI calls. The provider helpers below already
    create trusted raw logs inside the proxy. This function exists only for
    unusual/manual audit cases and never writes trusted cost rows to the raw
    export sheet.
    """
    try:
        r = _post(
            "/api/usage-log",
            google_token,
            {
                "app": app,
                "filename": filename,
                "task_id": task_id,
                "input_unit": input_unit,
                "count": count,
                "items": items,
                "video_duration": video_duration,
            },
            _TIMEOUT,
        )
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


class UsageSession:
    """Backward-compatible task context.

    Old kit versions used UsageSession to suppress per-call logging and write a
    combined manual row on flush(). In v2, the proxy logs every raw provider
    call. This class now only provides one shared task_id plus optional default
    metadata for old code that already passes session=.
    """

    def __init__(
        self,
        google_token,
        *,
        filename: str = "",
        input_unit: str = "",
        count: Any = None,
        video_duration: str = "",
        app: str = APP_NAME,
        task_id: str = "",
    ):
        self.token = google_token
        self.task_id = task_id or new_task_id()
        self.filename = filename
        self.input_unit = input_unit
        self.count = count
        self.video_duration = video_duration
        self.app = app

    def add(self, model="", tokens_in=0, tokens_out=0, cost_inr=None):
        return None

    def flush(self):
        """Compatibility no-op. Raw provider rows are already logged."""
        return {
            "ok": True,
            "task_id": self.task_id,
            "note": "raw provider calls already logged by proxy",
        }


def _apply_task_context(payload: Dict[str, Any], session=None, task_id: str = "") -> None:
    if session is not None:
        if not task_id:
            task_id = getattr(session, "task_id", "")
        if payload.get("app") == APP_NAME and getattr(session, "app", APP_NAME) != APP_NAME:
            payload["app"] = getattr(session, "app")
        for key in ("filename", "input_unit", "video_duration"):
            if not payload.get(key) and getattr(session, key, ""):
                payload[key] = getattr(session, key)
        if (
            (payload.get("count") is None or payload.get("count") == "")
            and getattr(session, "count", None) is not None
        ):
            payload["count"] = getattr(session, "count")
    if task_id:
        payload["task_id"] = task_id


def _raise_proxy_error(name: str, r) -> None:
    if r.status_code != 200:
        raise PWAccessError(f"{name} proxy error {r.status_code}: {r.text[:300]}")


def _upload_large_json(google_token, app: str, request: dict, provider_name: str) -> str:
    import json

    request_bytes = json.dumps(request).encode("utf-8")
    if len(request_bytes) <= _LARGE_REQUEST_BYTES:
        return ""

    up = _post("/api/gemini/upload-url", google_token, {"app": app}, _TIMEOUT)
    if up.status_code != 200:
        raise PWAccessError(f"{provider_name} upload-url error {up.status_code}: {up.text[:300]}")

    pr = requests.put(
        up.json()["upload_url"],
        data=request_bytes,
        headers={"Content-Type": "application/json"},
        timeout=_AI_TIMEOUT,
    )
    if pr.status_code != 200:
        raise PWAccessError(f"{provider_name} upload error {pr.status_code}: {pr.text[:300]}")
    return pr.json().get("url", "")


# ---------------------------------------------------------------------------
# Media offload. Vercel bills every byte an app sends INTO the proxy, so
# shipping base64 images, PDFs and video *through* it is the single largest
# line on the hosting bill. Instead the kit uploads the raw bytes once,
# straight to the proxy's private Google storage bucket, and sends only a link:
#   Gemini       reads the file from Google storage itself
#   GPT, Claude  the proxy fetches it from storage (not billed as transfer)
# Files delete themselves after 7 days. Nothing in an app changes - keep
# sending inline data exactly as before and this swaps it underneath. With an
# older proxy, or on any failure, the request goes inline exactly as before.
# ---------------------------------------------------------------------------
# Per-part floor. Real payloads turned out to be many small page crops rather
# than a few big images (Module QC, 2026-09-07: ~967 KB spread thin), so a high
# floor silently offloaded nothing at all.
_MEDIA_OFFLOAD_MIN_BYTES = 40_000
# ...but each offloaded part costs two extra HTTP round trips, so a request is
# only worth offloading once its media adds up to something meaningful.
_MEDIA_OFFLOAD_MIN_TOTAL = 300_000
# Bound the added latency on pathological requests.
_MEDIA_OFFLOAD_MAX_PARTS = 24
_MEDIA_UPLOAD_WORKERS = 6
# What Google storage takes. Gemini reads all of these; GPT and Claude take
# the images and PDFs.
_STORAGE_TYPES = {
    "application/pdf", "image/png", "image/jpeg", "image/webp", "image/gif",
    "video/mp4", "video/quicktime", "video/webm", "video/mpeg",
    "audio/mpeg", "audio/wav", "audio/mp4", "audio/aac", "audio/ogg", "audio/flac",
}
_IMAGE_PDF_TYPES = {"application/pdf", "image/png", "image/jpeg", "image/webp", "image/gif"}
# The older proxy store (Vercel Blob): Gemini only, used when the proxy has no
# storage route yet, and only for these types.
_OFFLOADABLE = {"application/pdf", "image/png", "image/jpeg", "image/webp"}
_DATA_URL = re.compile(r"^data:([\w/+.-]+);base64,(.*)$", re.S)
_storage_route_missing = False


def _offload_note(msg: str) -> None:
    """One line so an offload that does nothing is visible instead of silent —
    the failure mode that made this take four database queries to find."""
    try:
        print(f"[pw_access] media offload: {msg}", flush=True)
    except Exception:
        pass


def _upload_to_storage(google_token, app: str, raw: bytes, content_type: str) -> str:
    """Upload one file straight to Google storage and return its gs:// link,
    or '' when the proxy has no storage route (older proxy) or it fails."""
    global _storage_route_missing
    if _storage_route_missing:
        return ""
    up = _post("/api/files/upload-url", google_token,
               {"app": app, "content_type": content_type}, _TIMEOUT)
    if up.status_code == 404:
        _storage_route_missing = True      # older proxy: stop asking this run
        return ""
    if up.status_code != 200:
        return ""
    info = up.json()
    gs_uri = info.get("gs_uri") or ""
    if not gs_uri:
        return ""
    pr = requests.put(info["upload_url"], data=raw,
                      headers={"Content-Type": content_type}, timeout=_AI_TIMEOUT)
    if pr.status_code not in (200, 201):
        _offload_note(f"storage upload failed {pr.status_code}: {pr.text[:120]}")
        return ""
    return gs_uri


def _upload_media(google_token, app: str, raw: bytes, content_type: str) -> str:
    """Upload one file to the older proxy store, return its pathname ('' if the
    proxy is older still). Fallback for Gemini when storage is unavailable."""
    up = _post("/api/gemini/upload-url", google_token,
               {"app": app, "content_type": content_type}, _TIMEOUT)
    if up.status_code != 200:
        return ""
    info = up.json()
    pathname = info.get("pathname") or ""
    if not pathname:
        return ""      # proxy predates media offload — caller keeps the inline path
    pr = requests.put(info["upload_url"], data=raw,
                      headers={"Content-Type": content_type}, timeout=_AI_TIMEOUT)
    if pr.status_code != 200:
        raise PWAccessError(f"media upload failed {pr.status_code}: {pr.text[:200]}")
    # The store returns where the object ACTUALLY landed, which need not match
    # the pathname we asked for. _upload_large_json has always used this value;
    # trusting the proposed pathname instead produced links to nothing and a
    # Vertex BadRequestError (2026-09-07). Prefer the authoritative location.
    try:
        body = pr.json()
    except Exception:
        body = {}
    return body.get("url") or body.get("pathname") or pathname


def _offload(google_token, app: str, found: list, label: str, blob_fallback: bool = False) -> int:
    """`found` lists every inline media part as (raw_bytes, mime, apply).
    Uploads the ones worth moving (size rules above), several at a time, and
    calls apply(ref) on each with {"gs_uri" | "pathname": ..., "mime_type": ...}.
    Returns how many parts moved. Never raises: a part that cannot be moved
    simply stays inline."""
    small = sum(1 for raw, _, _ in found if len(raw) < _MEDIA_OFFLOAD_MIN_BYTES)
    cands = [f for f in found if len(f[0]) >= _MEDIA_OFFLOAD_MIN_BYTES]
    if not cands:
        if small:
            _offload_note(f"{label}: nothing offloaded — {small} media part(s) "
                          f"below {_MEDIA_OFFLOAD_MIN_BYTES // 1000} KB")
        return 0
    total = sum(len(raw) for raw, _, _ in cands)
    if total < _MEDIA_OFFLOAD_MIN_TOTAL:
        _offload_note(f"{label}: skipped — only {total // 1024} KB of media, "
                      f"below the {_MEDIA_OFFLOAD_MIN_TOTAL // 1024} KB floor")
        return 0
    # Biggest first, so a capped request still sheds the most bytes.
    cands.sort(key=lambda f: len(f[0]), reverse=True)
    if len(cands) > _MEDIA_OFFLOAD_MAX_PARTS:
        _offload_note(f"{label}: capping at {_MEDIA_OFFLOAD_MAX_PARTS} of "
                      f"{len(cands)} parts to bound latency")
        cands = cands[:_MEDIA_OFFLOAD_MAX_PARTS]

    def store(item):
        raw, mime, _ = item
        try:
            gs_uri = _upload_to_storage(google_token, app, raw, mime)
            if gs_uri:
                return {"gs_uri": gs_uri, "mime_type": mime}
            if blob_fallback and mime in _OFFLOADABLE:
                pathname = _upload_media(google_token, app, raw, mime)
                if pathname:
                    return {"pathname": pathname, "mime_type": mime}
        except Exception as e:
            _offload_note(f"{label}: a part stayed inline after an error: {e}")
        return None

    with ThreadPoolExecutor(max_workers=_MEDIA_UPLOAD_WORKERS) as pool:
        refs = list(pool.map(store, cands))
    moved = [(item, ref) for item, ref in zip(cands, refs) if ref]
    for (raw, mime, apply), ref in moved:
        apply(ref)
    if moved:
        kb = sum(len(item[0]) for item, _ in moved) // 1024
        where = "Google storage" if "gs_uri" in moved[0][1] else "the proxy store"
        _offload_note(f"{label}: offloaded {len(moved)} part(s), {kb} KB now read from {where}")
    else:
        _offload_note(f"{label}: nothing offloaded (older proxy or upload failed); "
                      "sending inline as before")
    return len(moved)


def _offload_media_parts(google_token, app: str, request: dict) -> dict:
    """Gemini: replace large inlineData parts with file_ref parts."""
    contents = (request or {}).get("contents") or []
    if not contents:
        return request
    try:
        out = copy.deepcopy(contents)
        found = []
        for content in out:
            for part in content.get("parts") or []:
                blob = (part or {}).get("inlineData") or (part or {}).get("inline_data")
                if not blob or not blob.get("data"):
                    continue
                mime = blob.get("mimeType") or blob.get("mime_type") or ""
                if mime not in _STORAGE_TYPES:
                    continue

                def apply(ref, part=part):
                    part.pop("inlineData", None)
                    part.pop("inline_data", None)
                    part["file_ref"] = ref
                found.append((base64.b64decode(blob["data"]), mime, apply))
        if not _offload(google_token, app, found, "gemini", blob_fallback=True):
            return request
        return {**request, "contents": out}
    except Exception as e:
        _offload_note(f"gemini: fell back to inline after an error: {e}")
        return request


def _offload_claude_media(google_token, app: str, request: dict) -> dict:
    """Claude: base64 image/document sources become gs:// url sources."""
    messages = (request or {}).get("messages") or []
    try:
        out = copy.deepcopy(messages)
        found = []
        for message in out:
            content = message.get("content") if isinstance(message, dict) else None
            for part in content if isinstance(content, list) else []:
                src = (part or {}).get("source") or {}
                if part.get("type") not in ("image", "document") or src.get("type") != "base64":
                    continue
                mime = src.get("media_type") or ""
                if mime not in _IMAGE_PDF_TYPES or not src.get("data"):
                    continue

                def apply(ref, part=part):
                    part["source"] = {"type": "url", "url": ref["gs_uri"]}
                found.append((base64.b64decode(src["data"]), mime, apply))
        if not _offload(google_token, app, found, "claude"):
            return request
        return {**request, "messages": out}
    except Exception as e:
        _offload_note(f"claude: fell back to inline after an error: {e}")
        return request


def _offload_openai_media(google_token, app: str, request: dict) -> dict:
    """GPT: data-URL images and PDF files become gs:// links."""
    messages = (request or {}).get("messages") or []
    try:
        out = copy.deepcopy(messages)
        found = []
        for message in out:
            content = message.get("content") if isinstance(message, dict) else None
            for part in content if isinstance(content, list) else []:
                kind = (part or {}).get("type")
                if kind == "image_url":
                    m = _DATA_URL.match(((part.get("image_url") or {}).get("url")) or "")
                    if not m or m.group(1) not in _IMAGE_PDF_TYPES:
                        continue

                    def apply(ref, part=part):
                        part["image_url"] = {**part["image_url"], "url": ref["gs_uri"]}
                    found.append((base64.b64decode(m.group(2)), m.group(1), apply))
                elif kind == "file":
                    f = part.get("file") or {}
                    m = _DATA_URL.match(f.get("file_data") or "")
                    if not m or m.group(1) not in _IMAGE_PDF_TYPES:
                        continue

                    def apply(ref, part=part, name=f.get("filename")):
                        part["file"] = {"file_id": ref["gs_uri"], **({"filename": name} if name else {})}
                    found.append((base64.b64decode(m.group(2)), m.group(1), apply))
        if not _offload(google_token, app, found, "gpt"):
            return request
        return {**request, "messages": out}
    except Exception as e:
        _offload_note(f"gpt: fell back to inline after an error: {e}")
        return request

def gemini_generate(
    google_token,
    *,
    model: str,
    request: dict,
    filename: str = "",
    input_unit: str = "",
    count: Any = None,
    task_id: str = "",
    video_duration: str = "",
    app: str = APP_NAME,
    session: UsageSession = None,
) -> dict:
    """Call Gemini through the proxy. Returns a Gemini-shaped result."""
    payload = {
        "app": app,
        "model": model,
        "request": request,
        "filename": filename,
        "input_unit": input_unit,
        "count": count,
        "video_duration": video_duration,
    }
    _apply_task_context(payload, session, task_id)
    # Send big media by reference; only then decide if the remainder still needs
    # the whole-request detour (usually it no longer does).
    request = _offload_media_parts(google_token, payload["app"], request)
    payload["request"] = request
    blob_url = _upload_large_json(google_token, payload["app"], request, "gemini")
    if blob_url:
        payload.pop("request")
        payload["request_blob_url"] = blob_url

    r = _post("/api/gemini/generate", google_token, payload, _AI_TIMEOUT)
    _raise_proxy_error("gemini", r)
    return r.json()


def mathpix_ocr(
    google_token,
    *,
    request: dict,
    filename: str = "",
    count: Any = 1,
    task_id: str = "",
    video_duration: str = "",
    app: str = APP_NAME,
    session: UsageSession = None,
) -> dict:
    """Call Mathpix OCR through the proxy."""
    payload = {
        "app": app,
        "request": request,
        "filename": filename,
        "count": count,
        "video_duration": video_duration,
    }
    _apply_task_context(payload, session, task_id)
    r = _post("/api/mathpix/ocr", google_token, payload, _AI_TIMEOUT)
    _raise_proxy_error("mathpix", r)
    return r.json()


def sarvam_tts(
    google_token,
    *,
    request: dict,
    filename: str = "",
    count: Any = None,
    task_id: str = "",
    video_duration: str = "",
    app: str = APP_NAME,
    session: UsageSession = None,
) -> dict:
    """Call Sarvam TTS through the proxy."""
    payload = {
        "app": app,
        "request": request,
        "filename": filename,
        "count": count,
        "video_duration": video_duration,
    }
    _apply_task_context(payload, session, task_id)
    r = _post("/api/sarvam/tts", google_token, payload, _AI_TIMEOUT)
    _raise_proxy_error("sarvam", r)
    return r.json()


def sarvam_chat(
    google_token,
    *,
    request: dict,
    model: str = "sarvam-105b",
    api_version: str = "v1",
    filename: str = "",
    input_unit: str = "",
    count: Any = None,
    task_id: str = "",
    video_duration: str = "",
    app: str = APP_NAME,
    session: UsageSession = None,
) -> dict:
    """Call a Sarvam chat model through the proxy.

    `request` is the raw Sarvam chat body - OpenAI-compatible, so
    {"messages": [...], "temperature": 0.2, "max_tokens": 2048}. It is sent
    unchanged and the reply comes back unchanged under result["result"].

    Models: sarvam-105b (default), gemma4, glm5.3, glm5.3-flash,
    deepseekv4.1-flash. api_version="v2" opts into Sarvam's beta API and
    needs beta access on the account; leave it at "v1" unless you know
    otherwise.

    Streaming is not supported - the proxy answers with one JSON object.
    """
    payload = {
        "app": app,
        "model": model,
        "api_version": api_version,
        "request": request,
        "filename": filename,
        "input_unit": input_unit,
        "count": count,
        "video_duration": video_duration,
    }
    _apply_task_context(payload, session, task_id)
    blob_url = _upload_large_json(google_token, payload["app"], request, "sarvam")
    if blob_url:
        payload.pop("request")
        payload["request_blob_url"] = blob_url

    r = _post("/api/sarvam/chat", google_token, payload, _AI_TIMEOUT)
    _raise_proxy_error("sarvam", r)
    return r.json()


def elevenlabs_tts(
    google_token,
    *,
    voice_id: str,
    request: dict,
    output_format: str = "mp3_44100_128",
    filename: str = "",
    count: Any = None,
    task_id: str = "",
    video_duration: str = "",
    app: str = APP_NAME,
    session: UsageSession = None,
) -> dict:
    """Call ElevenLabs TTS through the proxy."""
    payload = {
        "app": app,
        "voice_id": voice_id,
        "request": request,
        "output_format": output_format,
        "filename": filename,
        "count": count,
        "video_duration": video_duration,
    }
    _apply_task_context(payload, session, task_id)
    r = _post("/api/elevenlabs/tts", google_token, payload, _AI_TIMEOUT)
    _raise_proxy_error("elevenlabs", r)
    return r.json()


_AUDIO_MIME_BY_EXT = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".flac": "audio/flac",
}

# Over this, an inline clip would brush Vercel's ~4.5 MB inbound body cap once
# base64 adds its third, so we refuse instead of letting it fail as a 413.
_INLINE_AUDIO_LIMIT = 3 * 1024 * 1024


def _audio_mime(path: str) -> str:
    ext = os.path.splitext(str(path))[1].lower()
    mime = _AUDIO_MIME_BY_EXT.get(ext)
    if not mime:
        raise PWAccessError(
            "cannot tell the audio type of '%s' - pass "
            "{'bytes': ..., 'mime_type': 'audio/mpeg'} instead" % path
        )
    return mime


def upload_audio(
    google_token,
    source,
    *,
    content_type: str = "",
    app: str = APP_NAME,
) -> str:
    """Upload one audio clip to Google storage, return its gs:// link.

    source is a local file path or raw bytes (bytes need content_type).
    The link is what the ElevenLabs voice routes want: the bytes go straight to
    the bucket, so they never pass through Vercel and are never billed as
    origin transfer. Files delete themselves after 7 days.
    """
    if isinstance(source, (bytes, bytearray)):
        raw = bytes(source)
        if not content_type:
            raise PWAccessError("content_type is required when uploading raw bytes")
        mime = content_type
    else:
        mime = content_type or _audio_mime(source)
        with open(source, "rb") as fh:
            raw = fh.read()
    gs_uri = _upload_to_storage(google_token, app, raw, mime)
    if not gs_uri:
        raise PWAccessError("proxy storage upload failed - cannot send this clip")
    return gs_uri


def _audio_ref(google_token, app: str, item, label: str) -> dict:
    """One caller-supplied clip -> a reference the proxy accepts.

    Takes a local file path, a dict of raw bytes, or a dict already shaped for
    the proxy, and prefers the storage link every time. Inline base64 is only a
    fallback for a small clip against an older proxy.
    """
    if isinstance(item, dict):
        if item.get("gs_uri") or item.get("audio_base64"):
            return item                      # already a proxy reference
        raw = item.get("bytes")
        if raw is None:
            raise PWAccessError(
                "%s: give a file path, or a dict with 'bytes' + 'mime_type', "
                "or one with 'gs_uri'" % label
            )
        mime = item.get("mime_type") or ""
        if not mime:
            raise PWAccessError("%s: 'mime_type' is required with 'bytes'" % label)
        name = item.get("filename") or ""
        raw = bytes(raw)
    elif isinstance(item, (bytes, bytearray)):
        raise PWAccessError(
            "%s: raw bytes need a type - pass "
            "{'bytes': ..., 'mime_type': 'audio/mpeg'}" % label
        )
    else:
        mime = _audio_mime(item)       # reject a wrong type before any read
        with open(item, "rb") as fh:
            raw = fh.read()
        name = os.path.basename(str(item))

    gs_uri = _upload_to_storage(google_token, app, raw, mime)
    if gs_uri:
        ref = {"gs_uri": gs_uri, "mime_type": mime}
        if name:
            ref["filename"] = name
        return ref

    if len(raw) > _INLINE_AUDIO_LIMIT:
        raise PWAccessError(
            "%s: %.1f MB cannot be sent inline and the proxy storage route is "
            "unavailable - update the proxy" % (label, len(raw) / 1048576)
        )
    _offload_note("%s: storage unavailable, sending %d bytes inline" % (label, len(raw)))
    ref = {"audio_base64": base64.b64encode(raw).decode("ascii"), "mime_type": mime}
    if name:
        ref["filename"] = name
    return ref


def elevenlabs_similar_voices(
    google_token,
    *,
    audio,
    similarity_threshold: Any = None,
    top_k: Any = None,
    filename: str = "",
    task_id: str = "",
    app: str = APP_NAME,
    session: UsageSession = None,
) -> dict:
    """Find the closest Voice Library matches for a reference clip.

    audio is a local file path (or a dict - see _audio_ref). Returns the
    upstream body under ["result"]: {"voices": [...], "has_more": ...}. Each
    voice has a voice_id and a preview_url, so you can audition candidates and
    then pass the chosen voice_id to elevenlabs_tts().

    similarity_threshold is 0-2 and SMALLER means more similar; top_k is 1-100.
    """
    ref = _audio_ref(google_token, app, audio, "audio")
    payload = {
        "app": app,
        "audio": ref,
        "similarity_threshold": similarity_threshold,
        "top_k": top_k,
        "filename": filename or ref.get("filename", ""),
    }
    _apply_task_context(payload, session, task_id)
    r = _post("/api/elevenlabs/similar-voices", google_token, payload, _AI_TIMEOUT)
    _raise_proxy_error("elevenlabs", r)
    return r.json()


def elevenlabs_add_voice(
    google_token,
    *,
    name: str,
    files,
    consent: Dict[str, str],
    description: str = "",
    labels: Dict[str, str] = None,
    remove_background_noise: Any = None,
    filename: str = "",
    task_id: str = "",
    app: str = APP_NAME,
    session: UsageSession = None,
) -> dict:
    """Create an Instant Voice Clone in PW's ElevenLabs workspace.

    files is one local file path or a list of them (dicts also accepted - see
    _audio_ref). consent must say whose voice this is and who confirmed they
    agreed; the proxy records both against the new voice_id:

        consent={"speaker": "Atul Kumar (Physics faculty)",
                 "authorized_by": "atul.kumar@pw.live"}

    Returns {"voice_id": ..., "requires_verification": bool, ...}. CHECK
    requires_verification before using the voice: when it is true, ElevenLabs
    will not synthesise with it until it has been verified in the dashboard,
    and elevenlabs_tts() will fail. The response then carries a "note" saying so.
    """
    items = list(files) if isinstance(files, (list, tuple)) else [files]
    if not items:
        raise PWAccessError("files: at least one audio sample is required")
    refs = [
        _audio_ref(google_token, app, item, "files[%d]" % i)
        for i, item in enumerate(items)
    ]
    payload = {
        "app": app,
        "name": name,
        "files": refs,
        "consent": consent,
        "description": description,
        "labels": labels,
        "remove_background_noise": remove_background_noise,
        "filename": filename or refs[0].get("filename", ""),
    }
    _apply_task_context(payload, session, task_id)
    r = _post("/api/elevenlabs/voices/add", google_token, payload, _AI_TIMEOUT)
    _raise_proxy_error("elevenlabs", r)
    return r.json()


def claude_generate(
    google_token,
    *,
    model: str,
    request: dict,
    filename: str = "",
    input_unit: str = "",
    count: Any = None,
    task_id: str = "",
    video_duration: str = "",
    app: str = APP_NAME,
    session: UsageSession = None,
) -> dict:
    """Call Claude through the proxy."""
    payload = {
        "app": app,
        "model": model,
        "request": request,
        "filename": filename,
        "input_unit": input_unit,
        "count": count,
        "video_duration": video_duration,
    }
    _apply_task_context(payload, session, task_id)
    # Send big images/PDFs by link; only then decide if the remainder still
    # needs the whole-request detour (usually it no longer does).
    request = _offload_claude_media(google_token, payload["app"], request)
    payload["request"] = request
    blob_url = _upload_large_json(google_token, payload["app"], request, "claude")
    if blob_url:
        payload.pop("request")
        payload["request_blob_url"] = blob_url

    r = _post("/api/claude/generate", google_token, payload, _AI_TIMEOUT)
    _raise_proxy_error("claude", r)
    return r.json()


def gemini_tts(
    google_token,
    *,
    text: str,
    voice: str = "Kore",
    model: str = "gemini-3.1-flash-tts-preview",
    filename: str = "",
    count: Any = None,
    task_id: str = "",
    video_duration: str = "",
    app: str = APP_NAME,
    session: UsageSession = None,
) -> dict:
    """Call Gemini TTS through the proxy."""
    payload = {
        "app": app,
        "model": model,
        "text": text,
        "voice": voice,
        "filename": filename,
        "count": count,
        "video_duration": video_duration,
    }
    _apply_task_context(payload, session, task_id)
    r = _post("/api/gemini/tts", google_token, payload, _AI_TIMEOUT)
    _raise_proxy_error("gemini tts", r)
    return r.json()


def gemini_image(
    google_token,
    *,
    prompt: str,
    model: str = "gemini-3.1-flash-image",
    filename: str = "",
    count: Any = 1,
    task_id: str = "",
    video_duration: str = "",
    app: str = APP_NAME,
    session: UsageSession = None,
) -> dict:
    """Call Gemini image generation through the proxy."""
    payload = {
        "app": app,
        "model": model,
        "prompt": prompt,
        "filename": filename,
        "count": count,
        "video_duration": video_duration,
    }
    _apply_task_context(payload, session, task_id)
    r = _post("/api/gemini/image", google_token, payload, _AI_TIMEOUT)
    _raise_proxy_error("gemini image", r)
    return r.json()


def gpt_generate(
    google_token,
    *,
    request: dict,
    model: str = "gpt-5.6-luna-ard-content",
    filename: str = "",
    input_unit: str = "",
    count: Any = None,
    task_id: str = "",
    video_duration: str = "",
    app: str = APP_NAME,
    session: UsageSession = None,
) -> dict:
    """Call a ChatGPT model through the proxy.

    `request` is a native OpenAI chat.completions body and is sent through
    unchanged, e.g. {"messages": [{"role": "user", "content": "hi"}]}. Images
    and PDFs go in as base64 data URLs in the usual OpenAI content-part form.
    Models: gpt-5.6-luna-ard-content | gpt-5.6-sol-ard-content |
    gpt-5.6-terra-ard-content. Check what is live: GET /api/gpt/models.
    """
    payload = {
        "app": app,
        "model": model,
        "request": request,
        "filename": filename,
        "input_unit": input_unit,
        "count": count,
        "video_duration": video_duration,
    }
    _apply_task_context(payload, session, task_id)
    # Send big images/PDFs by link; only then decide if the remainder still
    # needs the whole-request detour (usually it no longer does).
    request = _offload_openai_media(google_token, payload["app"], request)
    payload["request"] = request
    blob_url = _upload_large_json(google_token, payload["app"], request, "gpt")
    if blob_url:
        payload.pop("request")
        payload["request_blob_url"] = blob_url

    r = _post("/api/gpt/generate", google_token, payload, _AI_TIMEOUT)
    _raise_proxy_error("gpt", r)
    return r.json()


def gpt_tts(
    google_token,
    *,
    text: str,
    voice: str = "alloy",
    model: str = "tts-ard-content",
    response_format: str = "mp3",
    filename: str = "",
    count: Any = None,
    task_id: str = "",
    video_duration: str = "",
    app: str = APP_NAME,
    session: UsageSession = None,
) -> dict:
    """Call OpenAI text-to-speech through the proxy.

    Voices: alloy | echo | fable | onyx | nova | shimmer.
    Models: tts-ard-content (standard) | tts-hd-ard-content (higher quality).
    Returns {"result": {"audio_base64": ..., "content_type": ...}, ...}.
    """
    payload = {
        "app": app,
        "model": model,
        "text": text,
        "voice": voice,
        "response_format": response_format,
        "filename": filename,
        "count": count,
        "video_duration": video_duration,
    }
    _apply_task_context(payload, session, task_id)
    r = _post("/api/gpt/tts", google_token, payload, _AI_TIMEOUT)
    _raise_proxy_error("gpt tts", r)
    return r.json()


def gpt_image(
    google_token,
    *,
    prompt: str,
    model: str = "gpt-image-2-litellm",
    size: str = "",
    quality: str = "",
    filename: str = "",
    count: Any = 1,
    task_id: str = "",
    video_duration: str = "",
    app: str = APP_NAME,
    session: UsageSession = None,
) -> dict:
    """Call OpenAI image generation through the proxy.

    Returns {"result": {"images": [{"image_base64"|"url", "content_type"}],
    "revised_prompt": ...}, ...}. `count` is the number of images (n).
    """
    payload = {
        "app": app,
        "model": model,
        "prompt": prompt,
        "size": size,
        "quality": quality,
        "filename": filename,
        "count": count,
        "video_duration": video_duration,
    }
    _apply_task_context(payload, session, task_id)
    r = _post("/api/gpt/image", google_token, payload, _AI_TIMEOUT)
    _raise_proxy_error("gpt image", r)
    return r.json()


def gemini_native(
    google_token,
    *,
    model: str,
    request: dict,
    filename: str = "",
    input_unit: str = "",
    count: Any = None,
    task_id: str = "",
    video_duration: str = "",
    app: str = APP_NAME,
    session: UsageSession = None,
) -> dict:
    """Call Gemini WITHOUT the OpenAI translation.

    Use this when the request or reply needs Gemini-native fields that the
    normal path cannot carry: tools (codeExecution, grounding),
    audioTranscriptionConfig (word timestamps), safetySettings, speechConfig,
    cachedContent. `request` is a native generateContent body and is sent
    unchanged; `result` is the provider reply unchanged.

    gemini_generate() remains correct for ordinary text and image prompts.
    """
    payload = {
        "app": app,
        "model": model,
        "request": request,
        "filename": filename,
        "input_unit": input_unit,
        "count": count,
        "video_duration": video_duration,
    }
    _apply_task_context(payload, session, task_id)
    request = _offload_media_parts(google_token, payload["app"], request)
    payload["request"] = request
    blob_url = _upload_large_json(google_token, payload["app"], request, "gemini native")
    if blob_url:
        payload.pop("request")
        payload["request_blob_url"] = blob_url

    r = _post("/api/gemini/native", google_token, payload, _AI_TIMEOUT)
    _raise_proxy_error("gemini native", r)
    return r.json()
