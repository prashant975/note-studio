"""Google Drive media access: every page Drive puts between a link and the file.

The content team reported "notes are not generated even with correct links":
the run stopped at "Drive served an HTML page rather than the file". That one
sentence covered four different situations — the large-file virus-scan warning
(which the app should simply get past), a file shared only inside pw.live (which
the app cannot open), a used-up download quota, and a missing file — and told
the person the same thing for all of them. These tests pin each one apart.

All network is mocked. The page fixtures follow the markup Drive serves; the
form-follow itself was not re-run against live Drive here.
"""
from __future__ import annotations

from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from requests.cookies import RequestsCookieJar

from src.video_sources import SOURCE_GOOGLE_DRIVE, detect_video_source
from src.video_sources.errors import (
    DriveAccessError,
    DriveDownloadDisabled,
    DriveNotShared,
    DriveQuotaExceeded,
    DriveUnsupportedContent,
)
from src.video_sources.google_drive import (
    _MAX_CONFIRM_HOPS,
    extract_file_id,
    extract_resource_key,
    fetch_drive_transcript,
    resolve_drive_video,
)

FILE_ID = "1uPb_LWAD1K-_Y9deo7hlyHdrA48BzF9g"
SHARE_LINK = f"https://drive.google.com/file/d/{FILE_ID}/view?usp=sharing"
RESOURCE_KEY = "0-AbCdEf_gHiJkLmNoPq-12"
UUID = "5c2f6a1e-0b7d-4c41-9e3f-2a8d1b6c7e90"

#: The current large-file page on drive.usercontent.google.com.
SCAN_FORM_PAGE = f"""<!DOCTYPE html><html><head>
<title>Google Drive - Virus scan warning</title>
<meta http-equiv="content-type" content="text/html; charset=utf-8"/></head>
<body><div class="uc-main"><div id="uc-text">
<p class="uc-warning-caption">Google Drive can&#39;t scan this file for viruses.</p>
<p class="uc-warning-subcaption"><span class="uc-name-size">
<a href="/open?id={FILE_ID}">lecture_2h.mp4</a> (2.2G)</span> is too large for Google
to scan for viruses. Would you still like to download this file?</p>
<form id="download-form" action="https://drive.usercontent.google.com/download" method="get">
<input type="submit" id="uc-download-link" class="goog-inline-block jfk-button jfk-button-action" value="Download anyway"/>
<input type="hidden" name="id" value="{FILE_ID}">
<input type="hidden" name="export" value="download">
<input type="hidden" name="confirm" value="t">
<input type="hidden" name="uuid" value="{UUID}">
{{extra}}</form></div></div></body></html>"""

#: The older drive.google.com/uc page: a link with a one-off token.
OLD_SCAN_PAGE = f"""<html><head><title>Google Drive - Virus scan warning</title></head>
<body><p class="uc-warning-caption">Google Drive can&#39;t scan this file for viruses.</p>
<a id="uc-download-link" class="goog-inline-block jfk-button jfk-button-action"
   href="/uc?export=download&amp;confirm=Ab12&amp;id={FILE_ID}">Download anyway</a>
</body></html>"""

SIGN_IN_PAGE = """<!doctype html><html lang="en"><head>
<title>Sign in - Google Accounts</title></head><body>
<form method="post" action="https://accounts.google.com/v3/signin/_/AccountsSignInUi/data">
<input type="hidden" name="continue" value="https://drive.usercontent.google.com/download">
</form><div>Use your Google Account</div></body></html>"""

QUOTA_PAGE = """<html><head><title>Google Drive - Quota exceeded</title></head><body>
<p class="uc-error-caption">Sorry, you can&#39;t view or download this file at this time.</p>
<p class="uc-error-subcaption">Too many users have viewed or downloaded this file
recently. Please try accessing the file again later. If the file you are trying to
access is particularly large or is shared with many people, it may take up to 24
hours to be able to view or download the file.</p></body></html>"""

FILE_HEADERS = {
    "Content-Type": "video/mp4",
    "Content-Disposition": 'attachment; filename="lecture_2h.mp4"',
    "Content-Range": "bytes 0-1/2390594088",
}
HTML = {"Content-Type": "text/html; charset=utf-8"}


class FakeResponse:
    def __init__(self, status=200, headers=None, body="", url="", history=(),
                 cookies=None, json_data=None):
        self.status_code = status
        self.headers = headers or {}
        self.text = body
        self._body = body.encode("utf-8")
        self.url = url
        self.history = list(history)
        self.cookies = RequestsCookieJar()
        for name, value in (cookies or {}).items():
            self.cookies.set(name, value, domain=".drive.google.com", path="/")
        self._json = json_data
        self.content = b"{}" if json_data is not None else self._body

    def iter_content(self, chunk_size=1):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start:start + chunk_size]

    def json(self):
        return self._json

    def close(self):
        pass


class FakeDrive:
    """Stands in for requests.get: answers from (predicate, response) routes in
    order, and records every request so a test can see what was sent."""

    def __init__(self, *routes):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        for matches, response in self.routes:
            if matches(url, kwargs):
                return response
        raise AssertionError(f"unexpected request: {url.split('?')[0]}")

    def query(self, index: int) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(urlsplit(self.calls[index][0]).query).items()}


def _resolve(fake, url=SHARE_LINK, *, token="", service_account=None, user_token="",
             user_email=""):
    with patch("src.video_sources.google_drive.requests.get", fake), \
         patch("src.video_sources.google_drive._access_token", return_value=token), \
         patch("src.video_sources.google_drive._service_account_info",
               return_value=service_account):
        return resolve_drive_video(url, user_token=user_token, user_email=user_email)


def _download_url(**params) -> str:
    from urllib.parse import urlencode
    return "https://drive.usercontent.google.com/download?" + urlencode(params)


# ---------------------------------------------------------------------------
# The virus-scan warning is answered, not reported
# ---------------------------------------------------------------------------
def test_scan_warning_form_is_submitted_and_the_file_is_reached():
    """The reported failure for a large lecture: Drive answers the first request
    with its "can't scan this file" page. The app must submit the page's form —
    every hidden input, uuid included — exactly as the Download anyway button
    would, and stream the file from there."""
    first_url = _download_url(id=FILE_ID, export="download", confirm="t")
    fake = FakeDrive(
        (lambda url, kw: "uuid=" in url,
         FakeResponse(206, FILE_HEADERS)),
        (lambda url, kw: True,
         FakeResponse(200, HTML, SCAN_FORM_PAGE.format(extra=""), url=first_url)),
    )
    media = _resolve(fake)

    assert len(fake.calls) == 2
    sent = fake.query(1)
    assert sent == {"id": FILE_ID, "export": "download", "confirm": "t", "uuid": UUID}
    assert urlsplit(media.stream_url).hostname == "drive.usercontent.google.com"
    assert f"uuid={UUID}" in media.stream_url
    assert media.content_length == 2390594088
    assert media.source_type == SOURCE_GOOGLE_DRIVE
    assert "virus-scan" in media.access_note
    assert media.url == SHARE_LINK


def test_older_scan_page_link_and_its_cookie_are_followed():
    """The older page carries a one-off confirm token in a link, tied to a
    download_warning cookie. The cookie must go with the follow-up request and
    on to FFmpeg, which has no cookie jar of its own."""
    page_url = f"https://drive.google.com/uc?export=download&id={FILE_ID}"
    cookie = {f"download_warning_13058876669334088843_{FILE_ID}": "Ab12"}
    fake = FakeDrive(
        (lambda url, kw: "confirm=Ab12" in url,
         FakeResponse(200, FILE_HEADERS)),
        (lambda url, kw: True,
         FakeResponse(200, HTML, OLD_SCAN_PAGE, url=page_url, cookies=cookie)),
    )
    media = _resolve(fake)

    follow_url, follow_kwargs = fake.calls[1]
    assert urlsplit(follow_url).hostname == "drive.google.com"
    assert fake.query(1)["confirm"] == "Ab12"
    assert follow_kwargs["cookies"] == cookie
    assert "download_warning_" in media.headers["Cookie"]
    assert media.stream_url == follow_url


def test_a_confirm_token_only_in_the_cookie_is_still_used():
    page_url = f"https://drive.google.com/uc?export=download&id={FILE_ID}"
    bare_page = ("<html><head><title>Google Drive - Virus scan warning</title></head>"
                 "<body>Google Drive can't scan this file for viruses.</body></html>")
    fake = FakeDrive(
        (lambda url, kw: "confirm=Zz9" in url, FakeResponse(200, FILE_HEADERS)),
        (lambda url, kw: True,
         FakeResponse(200, HTML, bare_page, url=page_url,
                      cookies={"download_warning_1_x": "Zz9"})),
    )
    media = _resolve(fake)
    assert fake.query(1) == {"export": "download", "confirm": "Zz9", "id": FILE_ID}
    assert "confirm=Zz9" in media.stream_url


def test_a_scan_warning_that_never_clears_says_so_not_not_shared():
    """If Drive keeps showing the warning after it has been answered, the
    person must not be told to change the sharing — that is not the problem."""
    fake = FakeDrive(
        (lambda url, kw: True,
         FakeResponse(200, HTML, SCAN_FORM_PAGE.format(extra=""),
                      url="https://drive.usercontent.google.com/download")),
    )
    with pytest.raises(DriveAccessError) as exc:
        _resolve(fake)

    assert not isinstance(exc.value, DriveNotShared)
    assert "can't scan this file" in exc.value.user_message
    assert "virus-scan warning" in str(exc.value)
    assert len(fake.calls) == 1 + _MAX_CONFIRM_HOPS     # bounded, no loop


def test_a_download_form_pointing_off_drive_is_never_followed():
    page = SCAN_FORM_PAGE.format(extra="").replace(
        'action="https://drive.usercontent.google.com/download"',
        'action="https://evil.example.com/collect"')
    fake = FakeDrive(
        (lambda url, kw: True,
         FakeResponse(200, HTML, page, url="https://drive.usercontent.google.com/download")),
    )
    with pytest.raises(DriveAccessError):
        _resolve(fake)
    assert len(fake.calls) == 1
    assert all("evil.example.com" not in url for url, _ in fake.calls)


# ---------------------------------------------------------------------------
# Files the app is not allowed to read: a clear "not shared"
# ---------------------------------------------------------------------------
def test_sign_in_redirect_means_not_shared_and_says_what_to_do():
    """A pw.live-only ("secure") link: Drive sends an anonymous request to the
    Google sign-in page. The person must be told it is a sharing setting, why
    the link works for them, and exactly which setting to change."""
    download = _download_url(id=FILE_ID, export="download", confirm="t")
    sign_in = ("https://accounts.google.com/v3/signin/identifier?continue="
               "https%3A%2F%2Fdrive.usercontent.google.com%2Fdownload&service=wise")
    hop = FakeResponse(302, {"Location": sign_in}, url=download)
    fake = FakeDrive(
        (lambda url, kw: True,
         FakeResponse(200, HTML, SIGN_IN_PAGE, url=sign_in, history=[hop])),
    )
    with pytest.raises(DriveNotShared) as exc:
        _resolve(fake)

    message = exc.value.user_message
    assert "not shared publicly" in message
    assert "Anyone with the link" in message
    assert "pw.live" in message
    detail = str(exc.value)
    assert FILE_ID in detail
    assert "accounts.google.com" in detail
    assert "Sign in - Google Accounts" in detail
    assert "no Google Drive credentials are configured" in detail
    assert len(fake.calls) == 1        # a sign-in form is not a download form


def test_sign_in_page_is_recognised_from_the_page_alone():
    page = SIGN_IN_PAGE.replace("v3/signin/_/AccountsSignInUi/data", "ServiceLogin")
    fake = FakeDrive((lambda url, kw: True, FakeResponse(200, HTML, page)))
    with pytest.raises(DriveNotShared):
        _resolve(fake)


def test_http_403_means_not_shared():
    fake = FakeDrive((lambda url, kw: True, FakeResponse(403, {"Content-Type": "text/plain"})))
    with pytest.raises(DriveNotShared):
        _resolve(fake)


def test_not_shared_names_the_service_account_when_one_is_configured():
    """With credentials configured, the credential route is tried first; when it
    cannot see the file either, the message names the address to share with."""
    email = "notes-reader@upsc-notes.iam.gserviceaccount.com"
    sign_in = "https://accounts.google.com/v3/signin/identifier"
    fake = FakeDrive(
        (lambda url, kw: url.startswith("https://www.googleapis.com/"),
         FakeResponse(404, {"Content-Type": "application/json"})),
        (lambda url, kw: True, FakeResponse(200, HTML, SIGN_IN_PAGE, url=sign_in)),
    )
    with pytest.raises(DriveNotShared) as exc:
        _resolve(fake, token="ya29.token", service_account={"client_email": email})

    assert email in exc.value.user_message
    assert "service account cannot see it either" in str(exc.value)
    assert "HTTP 404" in str(exc.value)


# ---------------------------------------------------------------------------
# The other pages, each named for what it is
# ---------------------------------------------------------------------------
def test_quota_page_is_reported_as_a_quota_not_as_sharing():
    fake = FakeDrive((lambda url, kw: True, FakeResponse(200, HTML, QUOTA_PAGE)))
    with pytest.raises(DriveQuotaExceeded) as exc:
        _resolve(fake)
    assert "too many times" in exc.value.user_message
    assert "Quota exceeded" in str(exc.value)


def test_quota_page_after_the_scan_warning_is_still_a_quota():
    fake = FakeDrive(
        (lambda url, kw: "uuid=" in url, FakeResponse(200, HTML, QUOTA_PAGE)),
        (lambda url, kw: True, FakeResponse(200, HTML, SCAN_FORM_PAGE.format(extra=""))),
    )
    with pytest.raises(DriveQuotaExceeded):
        _resolve(fake)


def test_missing_file_is_reported_as_not_found():
    page = ("<html><head><title>Google Drive - 404</title></head><body>Sorry, the "
            "file you have requested does not exist.</body></html>")
    fake = FakeDrive((lambda url, kw: True, FakeResponse(404, HTML, page)))
    with pytest.raises(DriveAccessError) as exc:
        _resolve(fake)
    assert "does not exist" in exc.value.user_message


def test_no_connection_is_not_blamed_on_sharing():
    import requests

    def offline(url, **kw):
        raise requests.ConnectionError("Failed to resolve drive.usercontent.google.com")

    with pytest.raises(DriveAccessError) as exc:
        _resolve(offline)
    assert "could not connect" in exc.value.user_message
    assert "shared" not in exc.value.user_message


def test_download_disabled_by_the_owner_is_named():
    fake = FakeDrive(
        (lambda url, kw: url.startswith("https://www.googleapis.com/"),
         FakeResponse(200, {"Content-Type": "application/json"}, json_data={
             "id": FILE_ID, "mimeType": "video/mp4", "size": "2390594088",
             "capabilities": {"canDownload": False}})),
    )
    with pytest.raises(DriveDownloadDisabled) as exc:
        _resolve(fake, token="ya29.token")
    assert "turned off downloading" in exc.value.user_message


# ---------------------------------------------------------------------------
# resourcekey: part of the permission for older links, so never dropped
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("url,expected", [
    (f"https://drive.google.com/file/d/{FILE_ID}/view?usp=sharing&resourcekey={RESOURCE_KEY}",
     RESOURCE_KEY),
    (f"https://drive.google.com/open?id={FILE_ID}&resourcekey={RESOURCE_KEY}", RESOURCE_KEY),
    (f"https://drive.google.com/uc?id={FILE_ID}&export=download&resourcekey={RESOURCE_KEY}",
     RESOURCE_KEY),
    (f"drive.google.com/file/d/{FILE_ID}/view?resourcekey={RESOURCE_KEY}", RESOURCE_KEY),
    (SHARE_LINK, ""),
    # Anything that is not a plain key is refused rather than pasted into a URL.
    (f"https://drive.google.com/file/d/{FILE_ID}/view?resourcekey=0-ab%22%3E%3Cx", ""),
])
def test_resource_key_is_read_from_the_link(url, expected):
    assert extract_resource_key(url) == expected
    assert extract_file_id(url) == FILE_ID


def test_resource_key_reaches_the_download_and_the_scan_follow_up():
    link = f"https://drive.google.com/file/d/{FILE_ID}/view?usp=sharing&resourcekey={RESOURCE_KEY}"
    fake = FakeDrive(
        (lambda url, kw: "uuid=" in url, FakeResponse(200, FILE_HEADERS)),
        (lambda url, kw: True, FakeResponse(200, HTML, SCAN_FORM_PAGE.format(extra=""))),
    )
    media = _resolve(fake, link)

    assert fake.query(0)["resourcekey"] == RESOURCE_KEY
    # The form did not carry it, so it is added back rather than lost.
    assert fake.query(1)["resourcekey"] == RESOURCE_KEY
    assert f"resourcekey={RESOURCE_KEY}" in media.stream_url


def test_resource_key_in_the_form_is_kept_as_served():
    link = f"https://drive.google.com/file/d/{FILE_ID}/view?resourcekey={RESOURCE_KEY}"
    form_page = SCAN_FORM_PAGE.format(
        extra=f'<input type="hidden" name="resourcekey" value="{RESOURCE_KEY}">')
    fake = FakeDrive(
        (lambda url, kw: "uuid=" in url, FakeResponse(200, FILE_HEADERS)),
        (lambda url, kw: True, FakeResponse(200, HTML, form_page)),
    )
    _resolve(fake, link)
    assert parse_qs(urlsplit(fake.calls[1][0]).query)["resourcekey"] == [RESOURCE_KEY]


def test_resource_key_goes_to_the_drive_api_as_its_header():
    link = f"https://drive.google.com/file/d/{FILE_ID}/view?resourcekey={RESOURCE_KEY}"
    fake = FakeDrive(
        (lambda url, kw: url.startswith("https://www.googleapis.com/"),
         FakeResponse(200, {"Content-Type": "application/json"}, json_data={
             "id": FILE_ID, "mimeType": "video/mp4", "size": "2390594088",
             "capabilities": {"canDownload": True}})),
    )
    media = _resolve(fake, link, token="ya29.token")

    sent = fake.calls[0][1]["headers"]
    assert sent["X-Goog-Drive-Resource-Keys"] == f"{FILE_ID}/{RESOURCE_KEY}"
    # FFmpeg makes the media request, so it needs the same header.
    assert media.headers["X-Goog-Drive-Resource-Keys"] == f"{FILE_ID}/{RESOURCE_KEY}"
    assert media.headers["Authorization"] == "Bearer ya29.token"
    assert media.stream_url == (f"https://www.googleapis.com/drive/v3/files/{FILE_ID}"
                                "?alt=media&supportsAllDrives=true")


def test_resource_key_reaches_the_native_transcript_lookup():
    from src.video_sources.google_drive import DriveTranscriptUnavailable

    link = f"https://drive.google.com/file/d/{FILE_ID}/view?resourcekey={RESOURCE_KEY}"
    fake = FakeDrive((lambda url, kw: True,
                      FakeResponse(200, body="status=fail&reason=private")))
    with patch("src.video_sources.google_drive.requests.get", fake), \
         patch("src.video_sources.google_drive._access_token", return_value=""):
        with pytest.raises(DriveTranscriptUnavailable):
            fetch_drive_transcript(link)
    assert "/playback" in fake.calls[0][0]
    assert fake.calls[0][1]["headers"]["X-Goog-Drive-Resource-Keys"] == f"{FILE_ID}/{RESOURCE_KEY}"
    legacy_calls = [index for index, (url, _) in enumerate(fake.calls) if "get_video_info" in url]
    assert legacy_calls
    assert fake.query(legacy_calls[0])["resourcekey"] == RESOURCE_KEY


# ---------------------------------------------------------------------------
# Every common link shape
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("url", [
    f"https://drive.google.com/file/d/{FILE_ID}/view",
    f"https://drive.google.com/file/d/{FILE_ID}/view?usp=sharing",
    f"https://drive.google.com/file/d/{FILE_ID}/view?usp=drive_link",
    f"https://drive.google.com/file/d/{FILE_ID}/preview",
    f"https://drive.google.com/file/d/{FILE_ID}",
    f"https://drive.google.com/open?id={FILE_ID}",
    f"https://drive.google.com/open?id={FILE_ID}&usp=drive_fs",
    f"https://drive.google.com/uc?id={FILE_ID}&export=download",
    f"https://drive.google.com/uc?export=download&id={FILE_ID}",
    f"https://drive.google.com/u/0/uc?id={FILE_ID}&export=download",
    f"https://drive.google.com/a/pw.live/file/d/{FILE_ID}/view?usp=sharing",
    f"https://drive.google.com/a/pw.live/open?id={FILE_ID}",
    f"https://drive.usercontent.google.com/download?id={FILE_ID}&export=download&authuser=0",
    f"https://docs.google.com/file/d/{FILE_ID}/edit?usp=sharing",
    f"  https://drive.google.com/file/d/{FILE_ID}/view?usp=sharing  ",
])
def test_every_common_link_shape_gives_the_file_id(url):
    assert detect_video_source(url) == SOURCE_GOOGLE_DRIVE
    assert extract_file_id(url) == FILE_ID


# ---------------------------------------------------------------------------
# The signed-in person's own Drive access (src/drive_auth.py)
# ---------------------------------------------------------------------------
USER = "teacher@pw.live"
SIGN_IN = "https://accounts.google.com/v3/signin/identifier"
VIDEO_META = {"id": FILE_ID, "mimeType": "video/mp4", "size": "2390594088",
              "capabilities": {"canDownload": True}}


def test_a_pdf_in_the_video_field_is_identified_before_audio_processing():
    fake = FakeDrive(_api(200, {
        "id": FILE_ID, "mimeType": "application/pdf", "size": "4395857",
        "capabilities": {"canDownload": True},
    }))
    with pytest.raises(DriveUnsupportedContent) as error:
        _resolve(fake, user_token="ya29.user", user_email=USER)
    assert "PDF" in error.value.user_message
    assert "lecture file field" in error.value.user_message
    assert len(fake.calls) == 1


def test_a_public_pdf_is_identified_before_audio_processing():
    fake = FakeDrive((lambda url, kw: True, FakeResponse(200, {
        "Content-Type": "application/pdf", "Content-Disposition": 'attachment; filename="lecture.pdf"',
    })))
    with pytest.raises(DriveUnsupportedContent, match="application/pdf"):
        _resolve(fake)


@pytest.mark.parametrize("mime", ("application/octet-stream", "application/ogg", "application/x-matroska"))
def test_valid_recording_container_types_remain_available_for_the_audio_reader(mime):
    fake = FakeDrive(_api(200, {**VIDEO_META, "mimeType": mime}))
    media = _resolve(fake, user_token="ya29.user", user_email=USER)
    assert media.content_type == mime
    assert media.stream_url.startswith("https://www.googleapis.com/drive/v3/files/")


def _api(status, json_data=None):
    return (lambda url, kw: url.startswith("https://www.googleapis.com/"),
            FakeResponse(status, {"Content-Type": "application/json"}, json_data=json_data))


def _public_sign_in():
    return (lambda url, kw: True, FakeResponse(200, HTML, SIGN_IN_PAGE, url=SIGN_IN))


def test_a_pw_only_video_opens_with_the_signed_in_account():
    """The reported case: a link shared inside pw.live. The person's own Drive
    access reaches it, and the public route is never tried."""
    fake = FakeDrive(_api(200, VIDEO_META))
    media = _resolve(fake, user_token="ya29.user", user_email=USER)

    assert media.headers["Authorization"] == "Bearer ya29.user"
    assert media.access_note == "your PW Google sign-in"
    assert media.url == SHARE_LINK
    assert len(fake.calls) == 1


def test_the_signed_in_account_goes_before_a_service_account():
    fake = FakeDrive(_api(200, VIDEO_META))
    media = _resolve(fake, token="ya29.service", user_token="ya29.user", user_email=USER)
    assert media.headers["Authorization"] == "Bearer ya29.user"


def test_a_public_video_still_opens_when_the_account_cannot_see_it():
    fake = FakeDrive(_api(404), (lambda url, kw: True, FakeResponse(200, FILE_HEADERS)))
    media = _resolve(fake, user_token="ya29.user", user_email=USER)
    assert media.access_note == "public link"


def test_a_video_not_shared_with_the_account_says_whom_to_ask():
    fake = FakeDrive(_api(404), _public_sign_in())
    with pytest.raises(DriveNotShared) as exc:
        _resolve(fake, user_token="ya29.user", user_email=USER)
    message = exc.value.user_message
    assert f"not shared with your PW account ({USER})" in message
    assert "Viewer" in message and "Anyone with the link" in message
    assert f"your account {USER} cannot see it either" in str(exc.value)


def test_a_sign_in_without_drive_permission_says_sign_in_again():
    body = {"error": {"code": 403, "status": "PERMISSION_DENIED",
                      "errors": [{"reason": "insufficientPermissions"}],
                      "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}}
    fake = FakeDrive(_api(403, body), _public_sign_in())
    with pytest.raises(DriveNotShared) as exc:
        _resolve(fake, user_token="ya29.user", user_email=USER)
    assert "sign in again" in exc.value.user_message
    assert "see your Google Drive files" in exc.value.user_message


def test_the_drive_api_switched_off_is_named_for_the_admin():
    body = {"error": {"code": 403, "errors": [{"reason": "accessNotConfigured"}],
                      "details": [{"reason": "SERVICE_DISABLED"}]}}
    fake = FakeDrive(_api(403, body), _public_sign_in())
    with pytest.raises(DriveNotShared) as exc:
        _resolve(fake, user_token="ya29.user", user_email=USER)
    assert "Google Drive API" in exc.value.user_message
    assert "not enabled" in str(exc.value)


def test_a_refused_token_says_the_permission_expired():
    fake = FakeDrive(_api(401), _public_sign_in())
    with pytest.raises(DriveNotShared) as exc:
        _resolve(fake, user_token="ya29.user", user_email=USER)
    assert "expired" in exc.value.user_message and "sign in again" in exc.value.user_message


def test_without_drive_access_the_message_says_how_to_allow_it():
    fake = FakeDrive(_public_sign_in())
    with pytest.raises(DriveNotShared) as exc:
        _resolve(fake)
    message = exc.value.user_message
    assert "sign in again" in message and "allow the app to see your Google Drive" in message
    assert "the sign-in carries no Drive access" in str(exc.value)


def test_the_token_reaches_the_resolver_from_the_transcript_step(tmp_path):
    """run_pipeline -> acquire_transcript -> resolve_media_source -> Drive."""
    from src import transcript
    from src.video_sources.errors import DriveAccessError as Stop
    from src.video_sources.google_drive import DriveTranscriptUnavailable

    seen = {}

    def resolve(url, **kwargs):
        seen.update(kwargs)
        raise Stop("stop here")

    with patch("src.video_sources.google_drive.fetch_drive_transcript",
               side_effect=DriveTranscriptUnavailable("private")), \
         patch.object(transcript, "resolve_media_source", resolve):
        with pytest.raises(Stop):
            transcript.acquire_transcript(SHARE_LINK, work_dir=tmp_path, use_cache=False,
                                          drive_token="ya29.user", drive_account=USER)
    assert seen == {"drive_token": "ya29.user", "drive_account": USER}
