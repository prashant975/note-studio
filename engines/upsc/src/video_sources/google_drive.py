"""Google Drive lecture videos.

Two things live here, and the cheap one is tried first by the caller:

**Native transcript** (fetch_drive_transcript). Drive runs speech recognition
over uploaded videos and shows the result in its viewer's Transcript panel.
That panel is fed by the same timedtext service YouTube captions use, reached
through the player's playback metadata and timedtext caption service.
Reading it costs three small HTTP requests; transcribing the same lecture from
its audio costs streaming the whole 2.39 GB reference file plus an STT pass.
Only files the request can already see answer here — a private file's player
config simply refuses, and the caller falls back to the media route below.

**Media resolution** (resolve_drive_video). The signed-in person's own Drive
access goes first (granted at sign-in; see src/drive_auth.py), because it is the
route that reaches a file shared only inside PW or with that person. A
configured service account comes next. Otherwise the file has to be one its
owner already shared with "anyone with the link", which the public download
endpoint serves — straight away for a small file, and behind Drive's "can't
scan this file for viruses" page for a large one, whose "Download anyway" form
is submitted exactly as a browser would.

No route circumvents anything: the signed-in route sees exactly what that
person could open in their browser, the credential route what the service
account has been granted, and the public route only files already published. A file that is neither returns an error naming which of
those it was — see DriveAccessError and its subclasses — rather than a crash
or, worse, an empty transcript.

Like direct_video, nothing is downloaded: the resolved endpoint is handed to
FFmpeg, which reads the audio remotely.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from requests.cookies import RequestsCookieJar

from ..models import NormalizedTranscript, TranscriptSegment
from .direct_video import total_length
from .errors import (
    DriveAccessError,
    DriveDownloadDisabled,
    DriveNotShared,
    DriveQuotaExceeded,
    DriveUnsupportedContent,
)
from .source_router import _DRIVE_HOSTS, SOURCE_GOOGLE_DRIVE, MediaSource

log = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (compatible; UPSCNotes/1.0; +https://pw.live)"

#: Drive IDs are 25-44 chars in practice; the lower bound stays loose because
#: the format is not documented as fixed.
_ID = r"([A-Za-z0-9_-]{10,})"
_FILE_ID_PATTERNS = (
    re.compile(rf"/file/d/{_ID}"),
    re.compile(rf"/document/d/{_ID}"),
    re.compile(rf"/presentation/d/{_ID}"),
    re.compile(rf"/spreadsheets/d/{_ID}"),
    re.compile(rf"/d/{_ID}"),
    re.compile(rf"[?&]id={_ID}"),
)
_FOLDER_RE = re.compile(rf"/folders/{_ID}")
_RESOURCE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")

#: Scope is read-only on purpose. The app never needs to modify a lecture.
_DRIVE_SCOPES = ("https://www.googleapis.com/auth/drive.readonly",)

_PUBLIC_DOWNLOAD = "https://drive.usercontent.google.com/download"

#: How much of an HTML reply is read to tell which page it is. Drive's own pages
#: (scan warning, quota, not found) are a few KB and the sign-in page carries its
#: markers near the top; the cap only stops a mislabelled body being pulled whole.
_PAGE_PEEK_BYTES = 256 * 1024

#: The scan warning is one hop. A second warning after submitting the first
#: means Drive is not releasing the file, and looping would not change that.
_MAX_CONFIRM_HOPS = 2

_NO_CREDENTIALS = "no Google Drive credentials are configured"

# Page markers, matched against the unescaped, lower-cased page. Each is the
# wording of a page Drive really serves, kept short so a reworded sentence
# around it does not break the match.
_QUOTA_MARKERS = (
    "too many users have viewed or downloaded this file",
    "can't view or download this file at this time",
)
_SCAN_MARKERS = (
    "can't scan this file for viruses",
    "too large for google to scan",
    "virus scan warning",
)
_SIGN_IN_MARKERS = (
    "accounts.google.com/servicelogin",
    "accounts.google.com/v3/signin",
    "accounts.google.com/signin",
    "you need access",
    "request access",
    "you need permission",
)
_NOT_FOUND_MARKERS = (
    "file you have requested does not exist",
    "file does not exist",
)


def extract_file_id(url: str) -> str | None:
    """Pull the file ID out of any common Drive URL shape.

    Handles /file/d/ID/view (with ?usp=sharing or ?usp=drive_link, and under
    the /a/<domain>/ Workspace prefix), /open?id=ID, /uc?id=ID&export=download
    (also under /u/0/) and the drive.usercontent.google.com/download?id=ID form.
    """
    value = (url or "").strip()
    if not value:
        return None
    if _FOLDER_RE.search(value):
        # A folder is not a video, and guessing which file inside it the user
        # meant would be worse than saying so.
        return None
    for pattern in _FILE_ID_PATTERNS:
        match = pattern.search(value)
        if match:
            return match.group(1)
    return None


def extract_resource_key(url: str) -> str:
    """The `resourcekey=` a share link carries, or "".

    Links to files shared before Drive's 2021 security update carry one, and for
    those files it is part of the permission: a request without it is refused
    exactly as if the file were private. So it is read off the pasted link and
    sent with every request made for the file.
    """
    try:
        query = parse_qs(urlsplit((url or "").strip()).query)
    except ValueError:
        return ""
    for name in ("resourcekey", "resourceKey"):
        for value in query.get(name, []):
            if _RESOURCE_KEY_RE.fullmatch(value):
                return value
    return ""


def _service_account_info() -> dict | None:
    """Credentials, using the same env contract as src/usage_tracker.py.

    Deliberately reads the environment rather than importing usage_tracker: that
    module reaches into Streamlit secrets, and this one runs in the pipeline,
    which has no Streamlit context.
    """
    raw = (os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()
    if raw:
        try:
            if raw.startswith("{"):
                return json.loads(raw)
            path = Path(raw).expanduser()
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            log.warning("[VIDEO] GOOGLE_SERVICE_ACCOUNT_JSON could not be read: %s", e)
    path_text = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
    if path_text:
        try:
            path = Path(path_text).expanduser()
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            log.warning("[VIDEO] GOOGLE_APPLICATION_CREDENTIALS could not be read: %s", e)
    return None


def _access_token() -> str:
    """A Drive read-only bearer token, or "" when no credentials are configured.

    Never raises and never logs the token: a missing or broken service account
    just means the public route is tried instead.
    """
    info = _service_account_info()
    if not info:
        return ""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=list(_DRIVE_SCOPES))
        credentials.refresh(Request())
        return credentials.token or ""
    except Exception as e:  # noqa: BLE001 — credentials are optional by design
        log.warning("[VIDEO] Google credentials present but unusable: %s", e)
        return ""


def _service_account_email() -> str:
    """Who a file has to be shared with for the credential route to see it."""
    return str((_service_account_info() or {}).get("client_email") or "")


@dataclass
class _Miss:
    """Why the Drive API route did not hand over the file.

    ``kind`` decides what the person is told if the public route fails too:
    not_visible (that account cannot see the file), scope (the sign-in carries
    no Drive permission), expired (the token was refused), api_disabled (the
    app's Google project has the Drive API switched off), unreachable, http.
    """

    kind: str
    detail: str


#: Google's error reasons, from the "errors" list and the "details" list of a
#: Drive API error body. Both spellings occur depending on the endpoint.
_SCOPE_REASONS = ("insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT")
_API_DISABLED_REASONS = ("accessNotConfigured", "SERVICE_DISABLED")


def _api_error_reasons(response) -> str:
    try:
        body = response.json() or {}
    except ValueError:
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return ""
    entries = list(error.get("errors") or []) + list(error.get("details") or [])
    return " ".join(str(e.get("reason") or "") for e in entries if isinstance(e, dict))


def _require_recording_mime(mime: str, file_id: str) -> None:
    """Reject lecture decks before sending their bytes to the audio reader."""
    mime = mime.lower().split(";", 1)[0].strip()
    if (not mime or mime.startswith(("video/", "audio/"))
            or mime in {"application/octet-stream", "application/ogg", "application/x-ogg",
                        "application/x-matroska", "application/mxf", "application/vnd.ms-asf",
                        "application/vnd.apple.mpegurl", "application/x-mpegurl"}):
        return
    kind = "PDF" if mime == "application/pdf" else "non-video file"
    raise DriveUnsupportedContent(
        f"Drive file {file_id} is {mime}, not a lecture recording.",
        user_message=(
            f"The lecture video link points to a {kind}. Upload the lecture PDF/PPT "
            "in the lecture file field, and paste the recording's Drive link in "
            "the lecture video field."
        ),
    )


def _resolve_with_credentials(
    file_id: str, token: str, *, resource_key: str = "", timeout: int,
    who: str = "the configured service account",
) -> tuple[MediaSource | None, _Miss | None]:
    """Drive API v3 route, for a service account's token or the signed-in user's.

    Returns (media, None) when the file is reachable this way and (None, miss)
    when it is not. The miss goes into the error if the public route fails too,
    so the run log shows that every route was tried and what each one met.

    Uses requests + google-auth directly rather than google-api-python-client,
    which the project does not depend on and would not otherwise need.
    """
    headers = {"Authorization": f"Bearer {token}", "User-Agent": _UA}
    if resource_key:
        # The API's form of the link's resourcekey=. It rides in the headers, so
        # FFmpeg sends it on the media request as well as this one.
        headers["X-Goog-Drive-Resource-Keys"] = f"{file_id}/{resource_key}"
    meta_url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
    try:
        response = requests.get(
            meta_url, headers=headers, timeout=timeout,
            params={"fields": "id,name,mimeType,size,capabilities/canDownload",
                    "supportsAllDrives": "true"},
        )
    except requests.RequestException as e:
        log.warning("[VIDEO] Drive metadata request failed: %s", e)
        return None, _Miss("unreachable", f"the Drive API could not be reached with {who} ({e})")
    status = response.status_code
    if status in {401, 403, 404}:
        log.info("[VIDEO] Drive file %s not reachable with %s (HTTP %s); trying the "
                 "next route", file_id, who, status)
        reasons = _api_error_reasons(response) if status == 403 else ""
        if any(reason in reasons for reason in _API_DISABLED_REASONS):
            return None, _Miss("api_disabled", f"the Drive API is not enabled for the app's "
                                               f"Google project (HTTP 403 for {who})")
        if any(reason in reasons for reason in _SCOPE_REASONS):
            return None, _Miss("scope", f"{who} has not allowed the app to read Google "
                                        f"Drive (HTTP 403, insufficient scope)")
        if status == 401:
            return None, _Miss("expired", f"Google refused the Drive token of {who} (HTTP 401)")
        return None, _Miss("not_visible", f"{who} cannot see it either (Drive API HTTP {status})")
    if status >= 400:
        log.warning("[VIDEO] Drive metadata returned HTTP %s", status)
        return None, _Miss("http", f"the Drive API answered HTTP {status} for {who}")

    meta = response.json() if response.content else {}
    mime = str(meta.get("mimeType") or "")
    _require_recording_mime(mime, file_id)
    if (meta.get("capabilities") or {}).get("canDownload") is False:
        # Visible but not downloadable: the owner has switched downloading off
        # for viewers. alt=media would refuse the same way once FFmpeg asked,
        # and FFmpeg's wording for that is nothing a person could act on.
        raise DriveDownloadDisabled(
            f"Drive file {file_id} is visible to {who} but not downloadable by it "
            "(capabilities.canDownload is false).")
    try:
        size = int(meta.get("size") or 0)
    except (TypeError, ValueError):
        size = 0

    return MediaSource(
        source_type=SOURCE_GOOGLE_DRIVE,
        url="",
        stream_url=f"{meta_url}?alt=media&supportsAllDrives=true",
        headers=headers,
        file_id=file_id,
        content_type=mime,
        content_length=size,
        access_note="Google credentials",
    ), None


# ---------------------------------------------------------------------------
# Public route: the download endpoint, and every page Drive puts in front of it
# ---------------------------------------------------------------------------

def _download_url(file_id: str, resource_key: str = "") -> str:
    """The public download endpoint for one file.

    `confirm=t` pre-answers the large-file scan warning, which often saves the
    hop through that page; when Drive shows the page anyway, its own form is
    submitted instead (see _confirm_target).
    """
    params = {"id": file_id, "export": "download", "confirm": "t"}
    if resource_key:
        params["resourcekey"] = resource_key
    return f"{_PUBLIC_DOWNLOAD}?{urlencode(params)}"


@dataclass
class _Reply:
    """What one probe of a download URL came back with."""

    url: str
    status: int
    content_type: str = ""
    disposition: str = ""
    length: int = 0
    #: Every host the request passed through. A hop through accounts.google.com
    #: is the one unambiguous sign that Drive wanted a signed-in user.
    hosts: tuple[str, ...] = ()
    cookies: dict[str, str] = field(default_factory=dict)
    #: The start of the body, read only when Drive answered with HTML.
    page: str = ""

    @property
    def is_file(self) -> bool:
        # An HTML body with no attachment disposition is one of Drive's own
        # pages (sign-in, scan warning, quota), not the file.
        if self.status >= 400:
            return False
        return not ("text/html" in self.content_type.lower()
                    and "attachment" not in self.disposition.lower())


def _read_page(response) -> str:
    """The start of an HTML reply, decoded; at most _PAGE_PEEK_BYTES of it."""
    chunks: list[bytes] = []
    size = 0
    try:
        for chunk in response.iter_content(chunk_size=16 * 1024):
            if not isinstance(chunk, bytes):
                break
            chunks.append(chunk)
            size += len(chunk)
            if size >= _PAGE_PEEK_BYTES:
                break
    except (requests.RequestException, OSError) as e:
        # Whatever did arrive is still worth classifying; an empty page simply
        # reads as an unexplained one.
        log.info("[VIDEO] Drive page could not be read in full: %s", e)
    return b"".join(chunks)[:_PAGE_PEEK_BYTES].decode("utf-8", errors="replace")


def _hosts(urls: list) -> tuple[str, ...]:
    """The distinct hosts along a redirect chain, in the order visited."""
    hosts: list[str] = []
    for url in urls:
        if not isinstance(url, str):
            continue
        host = (urlsplit(url).hostname or "").lower()
        if host and (not hosts or hosts[-1] != host):
            hosts.append(host)
    return tuple(hosts)


def _cookies_of(chain: list, carried: dict[str, str]) -> dict[str, str]:
    """Cookies set anywhere along the redirect chain, on top of those sent."""
    cookies = dict(carried)
    for hop in chain:
        jar = getattr(hop, "cookies", None)
        if isinstance(jar, RequestsCookieJar):
            cookies.update(jar.get_dict())
    return cookies


def _probe(url: str, *, cookies: dict[str, str], timeout: int) -> _Reply:
    """One ranged GET: enough to tell the file from one of Drive's pages
    without pulling the file."""
    try:
        response = requests.get(
            url, headers={"User-Agent": _UA, "Range": "bytes=0-1"},
            cookies=cookies or None, timeout=timeout, allow_redirects=True, stream=True,
        )
    except requests.RequestException as e:
        raise DriveAccessError(
            f"Could not reach Google Drive: {e}",
            user_message=("The app could not connect to Google Drive. Please check "
                          "the internet connection and try again."),
        ) from e
    try:
        final_url = response.url if isinstance(response.url, str) and response.url else url
        history = list(getattr(response, "history", None) or [])
        content_type = response.headers.get("Content-Type", "")
        return _Reply(
            url=final_url,
            status=response.status_code,
            content_type=content_type,
            disposition=response.headers.get("Content-Disposition", ""),
            length=total_length(response.headers),
            hosts=_hosts([url, *(getattr(h, "url", "") for h in history), final_url]),
            cookies=_cookies_of([*history, response], cookies),
            page=_read_page(response) if "text/html" in content_type.lower() else "",
        )
    finally:
        response.close()


@dataclass
class _Form:
    id: str
    action: str
    inputs: dict[str, str] = field(default_factory=dict)


class _DrivePage(HTMLParser):
    """The parts of a Drive page that say what to do next: its forms with their
    hidden inputs, any old-style "Download anyway" link, and its title.

    A real parser rather than regexes, because the attribute order inside
    Drive's <input> tags is not something to depend on.
    """

    def __init__(self, page: str) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[_Form] = []
        self.links: list[str] = []
        self.title = ""
        self._form: _Form | None = None
        self._in_title = False
        self.feed(page or "")
        self.close()

    def handle_starttag(self, tag, attrs):
        attr = {name: value or "" for name, value in attrs}
        if tag == "form":
            self._form = _Form(attr.get("id", ""), attr.get("action", ""))
            self.forms.append(self._form)
        elif tag == "input" and self._form is not None:
            if attr.get("type", "").lower() == "hidden" and attr.get("name"):
                self._form.inputs[attr["name"]] = attr.get("value", "")
        elif tag == "a":
            href = attr.get("href", "")
            if attr.get("id") == "uc-download-link" or "confirm=" in href:
                self.links.append(href)
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def _confirm_target(reply: _Reply, page: _DrivePage, file_id: str, resource_key: str) -> str:
    """The URL Drive's "Download anyway" button would load, or "" if it has none.

    Two generations of the scan warning are handled:
      - current: a GET form (id="download-form") on drive.usercontent.google.com
        whose hidden inputs — id, export, confirm, uuid, and resourcekey when the
        file has one — are the whole request. They are sent back exactly as
        served: which of them Drive insists on has changed over time (a bare
        confirm=t used to be enough), so none is dropped or made up;
      - older: an <a id="uc-download-link"> whose href carries a one-off
        confirm= token, the same token Drive also sets as a download_warning_*
        cookie — used on its own when the page carries no link.
    Only a URL back on one of Drive's own hosts is ever returned.
    """
    candidates: list[str] = []
    for form in page.forms:
        if form.id == "download-form" or "confirm" in form.inputs:
            action = urlsplit(urljoin(reply.url, form.action or _PUBLIC_DOWNLOAD))
            query = dict(parse_qsl(action.query, keep_blank_values=True))
            query.update(form.inputs)
            candidates.append(urlunsplit(action._replace(query=urlencode(query))))
    candidates += [urljoin(reply.url, href) for href in page.links]
    token = next((value for name, value in reply.cookies.items()
                  if name.startswith("download_warning")), "")
    if token:
        candidates.append("https://drive.google.com/uc?" + urlencode(
            {"export": "download", "confirm": token, "id": file_id}))

    for candidate in candidates:
        parts = urlsplit(candidate)
        if parts.scheme != "https" or (parts.hostname or "").lower() not in _DRIVE_HOSTS:
            log.warning("[VIDEO] Ignoring a download link that leaves Drive (%s)",
                        parts.hostname or "no host")
            continue
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        if resource_key and not query.get("resourcekey"):
            query["resourcekey"] = resource_key
        return urlunsplit(parts._replace(query=urlencode(query)))
    return ""


def _not_shared(file_id: str, seen: str, *, credentials_note: str,
                share_with: str) -> DriveNotShared:
    user_message = DriveNotShared.user_message
    if share_with:
        user_message += (f" You can also share it with the app's own Google "
                         f"account, {share_with} (Viewer is enough).")
    return DriveNotShared(
        f"Drive would not serve file {file_id} without a sign-in ({seen}): it is "
        f"not shared with 'Anyone with the link', and {credentials_note}.",
        user_message=user_message,
    )


def _explain(reply: _Reply, page: _DrivePage, file_id: str, *, scan_stuck: bool,
             credentials_note: str, share_with: str) -> DriveAccessError:
    """Turn a reply that was not the file into the error that says which page
    Drive put in the way, and so what the person can do about it."""
    # The markers say "can't"; a page may spell it with the typographic
    # apostrophe (U+2019) instead.
    text = html.unescape(reply.page).lower().replace(chr(0x2019), "'")
    title = " ".join(page.title.split())[:80]
    seen = f"HTTP {reply.status}"
    if title:
        seen += f", page '{title}'"
    seen += f", via {' -> '.join(reply.hosts) or 'Drive'}"

    if any(marker in text for marker in _QUOTA_MARKERS):
        return DriveQuotaExceeded(
            f"Drive refused file {file_id}: its download quota is used up — too many "
            f"views or downloads recently ({seen}).")
    # Checked before the page text: once a request has been sent to the sign-in
    # host, nothing else on the page changes what the person has to do.
    if "accounts.google.com" in reply.hosts:
        return _not_shared(file_id, seen, credentials_note=credentials_note,
                           share_with=share_with)
    if scan_stuck or any(marker in text for marker in _SCAN_MARKERS):
        how = ("even after its 'Download anyway' form was submitted" if scan_stuck
               else "and the page offered no way past it on Drive's own hosts")
        return DriveAccessError(
            f"Drive answered file {file_id} with its virus-scan warning instead of "
            f"the file, {how} ({seen}).",
            user_message=(
                "Google Drive stopped this large video at its \"can't scan this file "
                "for viruses\" warning and would not release it to the app. Please "
                "try again in a few minutes; if it keeps happening, send the reason "
                "shown below to the developer."
            ),
        )
    if reply.status == 404 or any(marker in text for marker in _NOT_FOUND_MARKERS):
        return DriveAccessError(
            f"Drive says file {file_id} does not exist ({seen}); {credentials_note}.",
            user_message=(
                "Google Drive says this video does not exist, or it is not shared "
                "with the app. Please check that the link is complete and that the "
                "video is shared as \"Anyone with the link\"."
            ),
        )
    if reply.status in {401, 403} or any(marker in text for marker in _SIGN_IN_MARKERS):
        return _not_shared(file_id, seen, credentials_note=credentials_note,
                           share_with=share_with)
    if reply.page or "text/html" in reply.content_type.lower():
        return DriveAccessError(
            f"Drive served an HTML page rather than the file for {file_id} ({seen}); "
            f"{credentials_note}.",
            user_message=(
                "Google Drive sent back a web page instead of the video. Usually this "
                "means the video is not shared as \"Anyone with the link\", or its "
                "owner has turned off downloading for viewers. Please check both in "
                "Drive's Share settings, then paste the link again."
            ),
        )
    return DriveAccessError(
        f"Drive returned HTTP {reply.status} for file {file_id} ({seen}).",
        user_message=(
            f"Google Drive did not hand over the video (error {reply.status}). This "
            "is usually temporary; please try again in a few minutes."
        ),
    )


def _resolve_public(
    file_id: str, *, resource_key: str = "", timeout: int,
    credentials_note: str = _NO_CREDENTIALS, share_with: str = "",
) -> MediaSource:
    """Public "anyone with the link" route.

    A file Drive will not hand over answers with an HTML page instead of bytes,
    and which page it is (sign-in, quota, scan warning, not found) is what the
    error reports. The scan warning is the one page that is not a refusal — it
    is Drive asking a browser to confirm a large download — so it is answered
    and the probe repeated.
    """
    url = _download_url(file_id, resource_key)
    cookies: dict[str, str] = {}
    hops = 0
    while True:
        reply = _probe(url, cookies=cookies, timeout=timeout)
        cookies = reply.cookies
        if reply.is_file:
            _require_recording_mime(reply.content_type, file_id)
            headers = {"User-Agent": _UA}
            if hops and cookies:
                # A confirmation can be tied to the cookie Drive set alongside
                # the warning. FFmpeg keeps no cookie jar, so it gets them as a
                # header, which the extractor never logs.
                headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
            return MediaSource(
                source_type=SOURCE_GOOGLE_DRIVE,
                url="",
                stream_url=url,
                headers=headers,
                file_id=file_id,
                content_type=reply.content_type,
                content_length=reply.length,
                access_note=("public link, past Drive's virus-scan warning" if hops
                             else "public link"),
            )

        page = _DrivePage(reply.page)
        target = _confirm_target(reply, page, file_id, resource_key) if reply.status < 400 else ""
        if not target or hops >= _MAX_CONFIRM_HOPS:
            raise _explain(reply, page, file_id, scan_stuck=bool(target),
                           credentials_note=credentials_note, share_with=share_with)
        hops += 1
        log.info("[VIDEO] Drive showed its virus-scan warning for %s; submitting its "
                 "'Download anyway' form", file_id)
        url = target


#: What to do when a file is not public, by why the signed-in account's own
#: Drive route did not reach it. Without that route at all ("" key) it is the
#: DriveNotShared default: the sign-in has no Drive permission yet.
_PUBLIC_FALLBACK = ("Or, in Drive, share the video as \"Anyone with the link\" "
                    "(Viewer) and paste that link.")
_USER_ROUTE_ADVICE = {
    "not_visible": ("This Google Drive video is not shared with your PW account{account}, "
                    "so the app cannot open it with your sign-in either. Ask the person who "
                    "shared it to give your account access (Viewer is enough), then paste "
                    "the link again. " + _PUBLIC_FALLBACK),
    "scope": ("This Google Drive video is limited to PW or to named people, and your "
              "sign-in does not yet let the app read Google Drive. Sign out of the app and "
              "sign in again, and allow the app to see your Google Drive files when "
              "Google asks. " + _PUBLIC_FALLBACK),
    "expired": ("This Google Drive video is limited to PW or to named people, and the "
                "app's permission to read your Google Drive has expired or was removed. "
                "Sign out of the app and sign in again, and allow Drive access when Google "
                "asks. " + _PUBLIC_FALLBACK),
    "api_disabled": ("This Google Drive video is limited to PW or to named people. Your "
                     "sign-in allows the app to read it, but the app's Google project "
                     "does not have the Google Drive API switched on yet. Ask whoever "
                     "manages the app's Google sign-in to enable \"Google Drive API\" for "
                     "that project. " + _PUBLIC_FALLBACK),
    "unreachable": ("This Google Drive video is not shared publicly, and the app could not "
                    "check it with your sign-in because Google Drive did not answer. "
                    "Please check the internet connection and try again. " + _PUBLIC_FALLBACK),
    "http": ("This Google Drive video is not shared publicly, and Google Drive refused "
             "the app's request made with your sign-in. Please try again in a few "
             "minutes. " + _PUBLIC_FALLBACK),
}


def resolve_drive_video(url: str, *, timeout: int = 30, user_token: str = "",
                        user_email: str = "") -> MediaSource:
    """Turn a Drive share link into streamable media, or explain why not.

    ``user_token`` is the signed-in person's own Drive read-only token (see
    src/drive_auth.py). It is tried first, because it opens exactly what that
    person could open in their browser — which is what a link "shared only
    inside PW" needs. A configured service account comes next, then the public
    "anyone with the link" route.
    """
    file_id = extract_file_id(url)
    if not file_id:
        if _FOLDER_RE.search(url or ""):
            raise DriveUnsupportedContent(
                f"{url} points at a Drive folder, not a file.",
                user_message=(
                    "That Google Drive link points at a folder. Please paste the link "
                    "to the lecture video file itself."
                ),
            )
        raise DriveAccessError(
            f"No Google Drive file ID could be read from {url!r}.",
            user_message="That Google Drive link does not contain a file ID.",
        )
    resource_key = extract_resource_key(url)
    log.info("[VIDEO] Google Drive file id: %s%s", file_id,
             " (the link carries a resource key)" if resource_key else "")

    notes: list[str] = []
    user_miss: _Miss | None = None
    if user_token:
        account = f"your account {user_email}" if user_email else "your account"
        media, user_miss = _resolve_with_credentials(
            file_id, user_token, resource_key=resource_key, timeout=timeout, who=account)
        if media is not None:
            media.url = url
            media.access_note = "your PW Google sign-in"
            log.info("[VIDEO] Drive file reachable with the signed-in account's Drive access")
            return media
        notes.append(user_miss.detail)
    else:
        notes.append("the sign-in carries no Drive access")

    token = _access_token()
    share_with = ""
    if token:
        media, miss = _resolve_with_credentials(
            file_id, token, resource_key=resource_key, timeout=timeout)
        if media is not None:
            media.url = url
            log.info("[VIDEO] Drive file reachable with configured credentials")
            return media
        notes.append(miss.detail)
        share_with = _service_account_email()
    else:
        notes.append(_NO_CREDENTIALS)

    try:
        media = _resolve_public(file_id, resource_key=resource_key, timeout=timeout,
                                credentials_note="; ".join(notes), share_with=share_with)
    except DriveNotShared as exc:
        if user_miss is None:
            raise
        # The default message asks for a sign-in with Drive access; this person
        # has one, so say what actually stood in the way.
        advice = _USER_ROUTE_ADVICE[user_miss.kind].format(
            account=f" ({user_email})" if user_email else "")
        if share_with:
            advice += (f" You can also share it with the app's own Google account, "
                       f"{share_with} (Viewer is enough).")
        raise DriveNotShared(str(exc), user_message=advice) from exc
    media.url = url
    log.info("[VIDEO] Drive file reachable as a %s (%s, %s bytes)", media.access_note,
             media.content_type or "no content-type", media.content_length or "unknown")
    return media


# ---------------------------------------------------------------------------
# Drive-native transcript (the viewer's Transcript panel, programmatically)
# ---------------------------------------------------------------------------

class DriveTranscriptUnavailable(RuntimeError):
    """No usable Drive-native transcript. NEVER user-facing on its own: the
    caller falls back to transcribing the audio, so this is a routing signal,
    not a failure of the run."""


#: Same order the YouTube adapter uses: PW lectures are usually Hindi-medium.
DRIVE_LANGUAGE_PREFERENCE = ("hi", "en")

_GVI_TIMEOUT = 30
#: A 2-hour lecture's track is ~220 KB of XML; a minute is a hang guard.
_TRACK_TIMEOUT = 60

# Public client identifier shipped by Drive's viewer (also used by yt-dlp's
# GoogleDrive extractor). It identifies the playback client; Google still
# checks the file's sharing permissions. It is not an AI/provider credential.
# https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/googledrive.py
_DRIVE_PLAYER_KEY = "AIzaSyDVQw45DwoYh632gvsP5vPDqEKvb-Ywnb8"


def _safe_tts(url: str) -> str:
    """timedtext URLs carry signed query parameters — never log them."""
    return (url or "").split("?")[0]


def _playback_problem(response) -> str:
    """Describe known failures without exposing Google's signed URLs or tokens."""
    reasons = _api_error_reasons(response)
    if "SERVICE_DISABLED" in reasons:
        return ("Drive's private transcript service is unavailable to this app's "
                "Google project; a transcript may still be visible in the browser")
    if "USER_PERMISSION_DENIED" in reasons:
        return "Drive's transcript service refused access to this recording"
    if any(reason in reasons for reason in _SCOPE_REASONS):
        return "the Google sign-in does not have permission to read this transcript"
    return f"Drive playback returned HTTP {response.status_code}"


def _video_info(file_id: str, *, resource_key: str = "",
                timeout: int = _GVI_TIMEOUT, user_token: str = "") -> dict[str, str]:
    """Use the current Drive player; retain the old player as a fallback."""
    endpoint = f"https://content-workspacevideo-pa.googleapis.com/v1/drive/media/{file_id}/playback"
    headers = {"User-Agent": _UA, "Referer": "https://drive.google.com/"}
    if resource_key:
        headers["X-Goog-Drive-Resource-Keys"] = f"{file_id}/{resource_key}"
    attempts = [(headers, {"key": _DRIVE_PLAYER_KEY})]
    token = user_token or _access_token()
    if token:
        # Google's viewer key belongs to Google's project, whereas the OAuth
        # token belongs to this app's project. Combining them is rejected with
        # HTTP 400 (CONSUMER_INVALID), even when the user can read the file.
        attempts.append(({**headers, "Authorization": f"Bearer {token}"}, {}))
    problem = "Drive playback metadata was unavailable"
    for attempt_headers, params in attempts:
        try:
            response = requests.get(
                endpoint, params=params,
                headers=attempt_headers, timeout=timeout,
            )
        except requests.RequestException as exc:
            problem = f"Drive playback request failed ({type(exc).__name__})"
            continue
        if response.status_code != 200:
            problem = _playback_problem(response)
            continue
        try:
            data = response.json()
        except (ValueError, TypeError):
            problem = "Drive playback returned invalid JSON"
            continue
        if not isinstance(data, dict) or not isinstance(data.get("mediaMetadata"), dict):
            problem = "Drive playback returned invalid metadata"
            continue
        details = data.get("timedTextDetails") or {}
        ttsurl = details.get("timedTextBaseUrl", "") if isinstance(details, dict) else ""
        return {
            "status": "ok",
            "ttsurl": ttsurl if isinstance(ttsurl, str) else "",
            "length_seconds": str(data["mediaMetadata"].get("duration") or "").removesuffix("s"),
        }
    try:
        return _legacy_video_info(
            file_id, resource_key=resource_key, timeout=timeout, user_token=user_token)
    except DriveTranscriptUnavailable as exc:
        raise DriveTranscriptUnavailable(f"{problem}; legacy lookup: {exc}") from exc


def _legacy_video_info(file_id: str, *, resource_key: str = "",
                       timeout: int = _GVI_TIMEOUT, user_token: str = "") -> dict[str, str]:
    """The Drive player's own bootstrap data for one file.

    Anonymous first, because that is how the viewer reaches a public-link file;
    if that refuses and Google credentials are configured, one retry carries
    them. A file neither can see raises DriveTranscriptUnavailable — the media
    route (which reports permission problems properly) is next anyway.
    """
    endpoint = f"https://drive.google.com/get_video_info?docid={file_id}"
    if resource_key:
        # Sent for the same reason as on the download route: for a pre-2021
        # link the key is part of the permission.
        endpoint += f"&{urlencode({'resourcekey': resource_key})}"
    attempts: list[dict[str, str]] = [{"User-Agent": _UA}]
    token = user_token or _access_token()
    if token:
        attempts.append({"User-Agent": _UA, "Authorization": f"Bearer {token}"})

    last_reason = "no attempt was made"
    for headers in attempts:
        try:
            response = requests.get(endpoint, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            last_reason = f"get_video_info request failed: {e}"
            continue
        if response.status_code != 200:
            last_reason = f"get_video_info returned HTTP {response.status_code}"
            if response.status_code == 404:
                # This retired endpoint cannot be repaired by another token.
                break
            continue
        fields = {k: v[0] for k, v in parse_qs(response.text).items() if v}
        if fields.get("status") != "ok":
            reason = fields.get("reason", "")[:120] or "status was not ok"
            last_reason = f"Drive player refused: {reason}"
            continue
        return fields
    raise DriveTranscriptUnavailable(last_reason)


def _pick_track(tracks: list[dict[str, str]],
                preference: tuple[str, ...]) -> dict[str, str] | None:
    """Prefer a human-made track, then ASR, in the caller's language order —
    the same policy the YouTube adapter applies for the same reason."""
    for want_asr in (False, True):
        for lang in preference:
            for t in tracks:
                is_asr = (t.get("kind") == "asr")
                if is_asr == want_asr and t.get("lang_code", "").startswith(lang):
                    return t
    for want_asr in (False, True):
        for t in tracks:
            if (t.get("kind") == "asr") == want_asr:
                return t
    return None


def _parse_track_xml(xml_text: str) -> list[TranscriptSegment]:
    """timedtext's default format: <transcript><text start dur>…</text>…"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise DriveTranscriptUnavailable(f"transcript track was not valid XML: {e}") from e
    segments: list[TranscriptSegment] = []
    for node in root.iter("text"):
        text = html.unescape((node.text or "")).strip()
        if not text:
            continue
        try:
            start = float(node.get("start") or 0.0)
            duration = float(node.get("dur") or 0.0)
        except ValueError:
            continue
        segments.append(TranscriptSegment(start=start, end=start + duration, text=text))
    # The service emits segments in order, but the notes depend on it, so it is
    # enforced rather than assumed.
    segments.sort(key=lambda s: s.start)
    return segments


def fetch_drive_transcript(
    url_or_file_id: str,
    *,
    preference: tuple[str, ...] = DRIVE_LANGUAGE_PREFERENCE,
    timeout: int = _GVI_TIMEOUT,
    user_token: str = "",
) -> NormalizedTranscript:
    """The transcript Drive already made for this video, or raise
    DriveTranscriptUnavailable so the caller can fall back to STT.

    Flow (all three are the requests the Drive viewer itself makes):
      Drive playback metadata -> timedTextBaseUrl
      ttsurl&type=list        ->  caption tracks
      ttsurl&type=track&…     ->  timed segments
    """
    file_id = extract_file_id(url_or_file_id) or (
        url_or_file_id if re.fullmatch(r"[A-Za-z0-9_-]{10,}", url_or_file_id or "") else None)
    if not file_id:
        raise DriveTranscriptUnavailable(f"no file ID in {url_or_file_id!r}")
    log.info("[DRIVE] File ID extracted: %s", file_id)

    info = _video_info(file_id, resource_key=extract_resource_key(url_or_file_id),
                       timeout=timeout, user_token=user_token)
    log.info("[DRIVE] Access validated (player answered for this file)")

    log.info("[DRIVE] Checking native transcript")
    ttsurl = info.get("ttsurl", "")
    if not ttsurl:
        raise DriveTranscriptUnavailable(
            "the player reports no transcript service for this video")

    caption_target = urlsplit(ttsurl)
    if (caption_target.scheme != "https"
            or caption_target.hostname not in {"drive.google.com", "docs.google.com", "video.google.com"}
            or not caption_target.path.endswith("/timedtext")):
        raise DriveTranscriptUnavailable("the player returned an invalid transcript service URL")

    headers = {"User-Agent": _UA}
    try:
        listing = requests.get(
            f"{ttsurl}{'&' if caption_target.query else '?'}{urlencode({'type': 'list', 'v': file_id})}",
            headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise DriveTranscriptUnavailable(f"track list request failed ({type(e).__name__})") from e
    if listing.status_code != 200:
        raise DriveTranscriptUnavailable(
            f"track list returned HTTP {listing.status_code}")
    try:
        track_root = ET.fromstring(listing.text)
    except ET.ParseError as e:
        raise DriveTranscriptUnavailable(f"track list was not valid XML: {e}") from e
    tracks = [dict(t.attrib) for t in track_root.iter("track")]
    if not tracks:
        raise DriveTranscriptUnavailable("no caption tracks published for this video")

    chosen = _pick_track(tracks, preference)
    if chosen is None:
        raise DriveTranscriptUnavailable("no usable caption track")
    lang = chosen.get("lang_code", "")
    log.info("[DRIVE] Native transcript found (lang=%s, kind=%s)",
             lang or "?", chosen.get("kind") or "manual")

    track_params = {"type": "track", "lang": lang, "v": file_id}
    if chosen.get("kind"):
        track_params["kind"] = chosen["kind"]
    if chosen.get("name"):
        track_params["name"] = chosen["name"]
    track_url = f"{ttsurl}{'&' if caption_target.query else '?'}{urlencode(track_params)}"
    try:
        track = requests.get(track_url, headers=headers, timeout=_TRACK_TIMEOUT)
    except requests.RequestException as e:
        raise DriveTranscriptUnavailable(f"track fetch failed ({type(e).__name__})") from e
    if track.status_code != 200:
        raise DriveTranscriptUnavailable(f"track fetch returned HTTP {track.status_code}")

    segments = _parse_track_xml(track.text)
    if not segments:
        raise DriveTranscriptUnavailable("the transcript track carried no text")
    log.info("[DRIVE] Transcript segments retrieved: %d (%s)",
             len(segments), _safe_tts(ttsurl))

    try:
        duration = float(info.get("length_seconds") or 0.0)
    except ValueError:
        duration = 0.0
    kind = "auto-generated" if chosen.get("kind") == "asr" else "manual"
    full_text = re.sub(r"\s+", " ", " ".join(s.text for s in segments)).strip()
    log.info("[DRIVE] Skipping audio extraction")
    log.info("[DRIVE] Skipping speech-to-text")
    return NormalizedTranscript(
        full_text=full_text,
        source_type=SOURCE_GOOGLE_DRIVE,
        source_label=f"Google Drive transcript ({lang or 'unknown'}, {kind})",
        language=lang,
        engine="drive_native",
        segments=segments,
        duration_seconds=duration or (segments[-1].end if segments else 0.0),
    )
