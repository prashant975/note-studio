"""Offline tests for the QC fetch layer: url_safety, page_fetcher, pdf_fetcher.

Every test is deterministic and network-free: socket.getaddrinfo is
monkeypatched, and HTTP traffic goes through fake session objects.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import socket

import pytest

from src.qc.page_fetcher import USER_AGENT, fetch_page
from src.qc.pdf_fetcher import fetch_pdf
from src.qc.url_safety import is_approved_url, validate_url_for_fetch

try:
    import fitz

    HAVE_FITZ = True
except Exception:
    HAVE_FITZ = False

APPROVED = ["pib.gov.in", "prsindia.org"]
# A genuinely internet-routable address. TEST-NET ranges (192.0.2.x etc.)
# deliberately do NOT work here: ipaddress marks them non-global and the
# validator rejects them — which is correct behaviour, not a bug.
PUBLIC_IP = "93.184.216.34"


# ------------------------------------------------------------- fakes / helpers

def _public_dns(monkeypatch, address=PUBLIC_IP):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


def _no_dns(monkeypatch):
    """Any resolution attempt is a bug: raise something url_safety does NOT
    swallow so the test fails loudly instead of passing by accident."""

    def explode(*args, **kwargs):
        raise AssertionError("socket.getaddrinfo must not be called here")

    monkeypatch.setattr(socket, "getaddrinfo", explode)


class FakeResponse:
    def __init__(self, status_code=200, headers=None, body=b""):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body
        self.closed = False

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    """Maps exact URLs to canned responses; records every call."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses[url]


class PoisonedSession:
    """A session that must never be used (cache hits must skip the network)."""

    def get(self, *args, **kwargs):
        raise AssertionError("network must not be touched on a cache hit")


# ------------------------------------------------------------- is_approved_url

def test_exact_domain_and_subdomain_approved():
    assert is_approved_url("https://pib.gov.in/PressRelease.aspx", APPROVED)
    assert is_approved_url("https://www.pib.gov.in/page", APPROVED)
    assert is_approved_url("https://static.pib.gov.in/doc.pdf", APPROVED)


def test_approval_is_case_insensitive():
    assert is_approved_url("https://PIB.GOV.IN/x", APPROVED)
    assert is_approved_url("https://pib.gov.in/x", ["PIB.GOV.IN"])


def test_lookalike_domains_rejected():
    assert not is_approved_url("https://evilpib.gov.in.attacker.com/", APPROVED)
    assert not is_approved_url("https://notpib.gov.in/", APPROVED)
    assert not is_approved_url("https://pib.gov.in.evil.example/", APPROVED)


def test_garbage_and_empty_urls_rejected():
    assert not is_approved_url("", APPROVED)
    assert not is_approved_url("not a url at all", APPROVED)
    assert not is_approved_url("https://pib.gov.in/x", [])


# ------------------------------------------------------ validate_url_for_fetch

def test_https_with_public_resolution_is_ok(monkeypatch):
    _public_dns(monkeypatch)
    ok, reason = validate_url_for_fetch("https://pib.gov.in/PressRelease", APPROVED)
    assert ok, reason
    ok, reason = validate_url_for_fetch("https://pib.gov.in:443/x", APPROVED)
    assert ok, reason


def test_http_rejected_even_for_approved_domain(monkeypatch):
    _no_dns(monkeypatch)
    ok, reason = validate_url_for_fetch("http://pib.gov.in/x", APPROVED)
    assert not ok
    assert "scheme" in reason


def test_file_ftp_data_schemes_rejected(monkeypatch):
    _no_dns(monkeypatch)
    for url in (
        "file:///C:/Windows/win.ini",
        "ftp://pib.gov.in/pub/doc.pdf",
        "data:text/html,<h1>hi</h1>",
    ):
        ok, _reason = validate_url_for_fetch(url, APPROVED)
        assert not ok, url


def test_userinfo_rejected(monkeypatch):
    _no_dns(monkeypatch)
    ok, reason = validate_url_for_fetch("https://user:pass@pib.gov.in/x", APPROVED)
    assert not ok
    assert "userinfo" in reason


def test_nonstandard_port_rejected(monkeypatch):
    _no_dns(monkeypatch)
    ok, reason = validate_url_for_fetch("https://pib.gov.in:8443/x", APPROVED)
    assert not ok
    assert "port" in reason


def test_raw_private_ips_rejected_even_if_whitelisted(monkeypatch):
    _no_dns(monkeypatch)
    for host in ("127.0.0.1", "10.1.2.3", "169.254.169.254", "192.168.0.10", "[::1]"):
        bare = host.strip("[]")
        ok, reason = validate_url_for_fetch(f"https://{host}/x", [bare])
        assert not ok, host
        assert "ip" in reason.lower()


def test_localhost_resolving_to_loopback_rejected(monkeypatch):
    _public_dns(monkeypatch, address="127.0.0.1")
    ok, reason = validate_url_for_fetch("https://localhost/x", ["localhost"])
    assert not ok
    assert "non-public" in reason


def test_one_private_address_among_public_rejects(monkeypatch):
    def fake(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 443)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    ok, reason = validate_url_for_fetch("https://pib.gov.in/x", APPROVED)
    assert not ok
    assert "10.0.0.7" in reason


def test_dns_failure_returns_dns_reason_not_exception(monkeypatch):
    def fail(*args, **kwargs):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    ok, reason = validate_url_for_fetch("https://pib.gov.in/x", APPROVED)
    assert not ok
    assert reason.startswith("dns")


def test_unapproved_domain_rejected_before_dns(monkeypatch):
    _no_dns(monkeypatch)  # would raise if the validator resolved it
    ok, reason = validate_url_for_fetch("https://example.com/x", APPROVED)
    assert not ok
    assert "approved" in reason


# ------------------------------------------------------------------ fetch_page

HTML = (
    b"<html><head><title>Press Release &amp; Update</title>"
    b'<meta property="article:published_time" content="2026-01-15T10:00:00+05:30">'
    b"<script>alert('never in output')</script></head>"
    b"<body><nav>Home | About | Contact</nav><header>Site banner</header>"
    b"<h1>GDP Growth</h1><p>India&#39;s GDP grew 7.8% in FY 2025-26.</p>"
    b"<footer>Copyright footer</footer></body></html>"
)


def test_fetch_page_extracts_title_date_and_clean_text(monkeypatch):
    _public_dns(monkeypatch)
    url = "https://pib.gov.in/release"
    session = FakeSession({
        url: FakeResponse(200, {"Content-Type": "text/html; charset=utf-8"}, HTML),
    })
    r = fetch_page(url, APPROVED, session=session)
    assert r.error is None
    assert r.status == 200
    assert r.title == "Press Release & Update"
    assert r.publication_date == "2026-01-15T10:00:00+05:30"
    assert "GDP grew 7.8%" in r.text
    assert "alert(" not in r.text            # scripts stripped
    assert "Home | About" not in r.text      # nav stripped
    assert "Copyright footer" not in r.text  # footer stripped
    assert r.text.index("GDP Growth") < r.text.index("GDP grew")  # order kept
    assert r.retrieved_at
    # request hygiene: descriptive UA, no automatic redirects
    _url, kwargs = session.calls[0]
    assert kwargs["headers"]["User-Agent"] == USER_AGENT
    assert kwargs["allow_redirects"] is False


def test_fetch_page_follows_approved_redirect(monkeypatch):
    _public_dns(monkeypatch)
    session = FakeSession({
        "https://pib.gov.in/a": FakeResponse(301, {"Location": "https://www.pib.gov.in/b"}),
        "https://www.pib.gov.in/b": FakeResponse(
            200, {"Content-Type": "text/html"},
            b"<title>Landed</title><p>Body text here</p>",
        ),
    })
    r = fetch_page("https://pib.gov.in/a", APPROVED, session=session)
    assert r.error is None
    assert r.final_url == "https://www.pib.gov.in/b"
    assert r.title == "Landed"
    assert len(session.calls) == 2


def test_fetch_page_aborts_redirect_off_approved_list(monkeypatch):
    _public_dns(monkeypatch)
    session = FakeSession({
        "https://pib.gov.in/a": FakeResponse(302, {"Location": "https://evil.example.com/steal"}),
    })
    r = fetch_page("https://pib.gov.in/a", APPROVED, session=session)
    assert r.error is not None
    assert "approved" in r.error
    assert len(session.calls) == 1  # the offsite URL was never requested
    assert r.text == ""


def test_fetch_page_aborts_past_max_bytes(monkeypatch):
    _public_dns(monkeypatch)
    url = "https://pib.gov.in/huge"
    body = b"<html>" + b"x" * 500
    session = FakeSession({url: FakeResponse(200, {"Content-Type": "text/html"}, body)})
    r = fetch_page(url, APPROVED, session=session, max_bytes=100)
    assert r.error is not None
    assert "bytes" in r.error
    assert r.text == ""


def test_fetch_page_rejects_non_html_content_type(monkeypatch):
    _public_dns(monkeypatch)
    url = "https://pib.gov.in/api"
    session = FakeSession({
        url: FakeResponse(200, {"Content-Type": "application/json"}, b'{"a": 1}'),
    })
    r = fetch_page(url, APPROVED, session=session)
    assert r.error is not None
    assert "content type" in r.error


def test_fetch_page_reports_http_error_status(monkeypatch):
    _public_dns(monkeypatch)
    url = "https://pib.gov.in/gone"
    session = FakeSession({url: FakeResponse(404, {"Content-Type": "text/html"}, b"")})
    r = fetch_page(url, APPROVED, session=session)
    assert r.status == 404
    assert r.error is not None


def test_fetch_page_cache_round_trip(monkeypatch, tmp_path):
    _public_dns(monkeypatch)
    url = "https://pib.gov.in/cached"
    session = FakeSession({
        url: FakeResponse(200, {"Content-Type": "text/html"},
                          b"<title>T</title><p>cached body</p>"),
    })
    first = fetch_page(url, APPROVED, session=session, cache_dir=tmp_path)
    assert first.error is None
    assert first.from_cache is False

    second = fetch_page(url, APPROVED, session=PoisonedSession(), cache_dir=tmp_path)
    assert second.from_cache is True
    assert second.error is None
    assert second.text == first.text
    assert second.title == "T"


def test_fetch_page_refresh_bypasses_cache(monkeypatch, tmp_path):
    _public_dns(monkeypatch)
    url = "https://pib.gov.in/cached2"
    session = FakeSession({
        url: FakeResponse(200, {"Content-Type": "text/html"},
                          b"<title>Old</title><p>v1</p>"),
    })
    fetch_page(url, APPROVED, session=session, cache_dir=tmp_path)
    session2 = FakeSession({
        url: FakeResponse(200, {"Content-Type": "text/html"},
                          b"<title>New</title><p>v2</p>"),
    })
    r = fetch_page(url, APPROVED, session=session2, cache_dir=tmp_path, refresh=True)
    assert r.from_cache is False
    assert r.title == "New"
    assert len(session2.calls) == 1


# ------------------------------------------------------------------- fetch_pdf

def _make_pdf(lines, creation_date="D:20250405120000"):
    doc = fitz.open()
    page = doc.new_page()
    y = 72
    for line in lines:
        page.insert_text((72, y), line)
        y += 14
    doc.set_metadata({"creationDate": creation_date})
    data = doc.tobytes()
    doc.close()
    return data


@pytest.mark.skipif(not HAVE_FITZ, reason="pymupdf unavailable")
def test_fetch_pdf_sniffs_magic_despite_wrong_content_type(monkeypatch, tmp_path):
    _public_dns(monkeypatch)
    body = _make_pdf(["Article 368 lays down the amendment procedure."] * 10)
    url = "https://pib.gov.in/doc.pdf"
    session = FakeSession({
        url: FakeResponse(200, {"Content-Type": "application/octet-stream"}, body),
    })
    r = fetch_pdf(url, APPROVED, session=session, cache_dir=tmp_path)
    assert r.error is None
    assert r.page_count == 1
    assert "Article 368" in r.text_pages[0]
    assert r.scanned is False
    assert r.publication_date == "2025-04-05"
    assert r.path is not None and Path(r.path).exists()
    assert Path(r.path).read_bytes().startswith(b"%PDF-")


@pytest.mark.skipif(not HAVE_FITZ, reason="pymupdf unavailable")
def test_fetch_pdf_flags_textless_pdf_as_scanned(monkeypatch):
    _public_dns(monkeypatch)
    body = _make_pdf([])  # one page, zero text: a scan with no OCR layer
    url = "https://pib.gov.in/scan.pdf"
    session = FakeSession({url: FakeResponse(200, {"Content-Type": "application/pdf"}, body)})
    r = fetch_pdf(url, APPROVED, session=session)
    assert r.error is None
    assert r.scanned is True
    assert r.page_count == 1
    assert r.text_pages[0].strip() == ""  # nothing invented


@pytest.mark.skipif(not HAVE_FITZ, reason="pymupdf unavailable")
def test_fetch_pdf_cache_round_trip(monkeypatch, tmp_path):
    _public_dns(monkeypatch)
    body = _make_pdf(["Cached fact line."] * 10)
    url = "https://pib.gov.in/cache.pdf"
    session = FakeSession({url: FakeResponse(200, {"Content-Type": "application/pdf"}, body)})
    first = fetch_pdf(url, APPROVED, session=session, cache_dir=tmp_path)
    assert first.error is None
    assert first.from_cache is False

    second = fetch_pdf(url, APPROVED, session=PoisonedSession(), cache_dir=tmp_path)
    assert second.from_cache is True
    assert second.error is None
    assert second.text_pages == first.text_pages
    assert second.page_count == first.page_count
    assert second.path is not None and Path(second.path).exists()


def test_fetch_pdf_rejects_non_pdf_body(monkeypatch):
    _public_dns(monkeypatch)
    url = "https://pib.gov.in/fake.pdf"
    session = FakeSession({
        url: FakeResponse(200, {"Content-Type": "text/html"}, b"<html>not a pdf</html>"),
    })
    r = fetch_pdf(url, APPROVED, session=session)
    assert r.error is not None
    assert "pdf" in r.error.lower()
    assert r.text_pages == []


def test_fetch_pdf_corrupt_body_returns_error_not_exception(monkeypatch):
    _public_dns(monkeypatch)
    url = "https://pib.gov.in/broken.pdf"
    session = FakeSession({
        url: FakeResponse(200, {"Content-Type": "application/pdf"}, b"%PDF-junk not real"),
    })
    r = fetch_pdf(url, APPROVED, session=session)
    assert r.error is not None  # either 'could not be opened' or 'pymupdf unavailable'
    assert r.text_pages == []


def test_fetch_pdf_without_pymupdf_reports_unavailable(monkeypatch):
    _public_dns(monkeypatch)
    # None in sys.modules makes `import fitz` raise ImportError deterministically.
    monkeypatch.setitem(sys.modules, "fitz", None)
    url = "https://pib.gov.in/nofitz.pdf"
    session = FakeSession({
        url: FakeResponse(200, {"Content-Type": "application/pdf"}, b"%PDF-1.7 minimal"),
    })
    r = fetch_pdf(url, APPROVED, session=session)
    assert r.error == "pymupdf unavailable"


def test_fetch_pdf_aborts_redirect_off_approved_list(monkeypatch):
    _public_dns(monkeypatch)
    session = FakeSession({
        "https://pib.gov.in/r.pdf": FakeResponse(
            302, {"Location": "https://attacker.example/x.pdf"}),
    })
    r = fetch_pdf("https://pib.gov.in/r.pdf", APPROVED, session=session)
    assert r.error is not None
    assert "approved" in r.error
    assert len(session.calls) == 1


# --------------------------------------- regressions from the QC audit
# A parser-divergence finding: urlsplit() treats a backslash as part of the
# host, while requests/urllib3 follow WHATWG, cut the authority at the
# backslash, and connect somewhere else entirely. Approving a host that two
# parsers read differently makes the whole approved-domain gate meaningless.

def test_backslash_host_is_not_approved():
    assert not is_approved_url(r"https://evil.com\.pib.gov.in/", APPROVED)
    ok, reason = validate_url_for_fetch(r"https://evil.com\.pib.gov.in/", APPROVED)
    assert not ok
    assert "dns name" in reason


def test_mangled_ip_literal_host_is_rejected():
    r"""'169.254.169.254\.pib.gov.in' must not slip past the raw-IP guard.

    ip_address() cannot parse it, so the non-public-address branch never ran,
    while the request would have reached the cloud metadata endpoint.
    """
    for host in (r"169.254.169.254\.pib.gov.in", r"127.0.0.1\.pib.gov.in"):
        assert not is_approved_url(f"https://{host}/", APPROVED)
        ok, _ = validate_url_for_fetch(f"https://{host}/", APPROVED)
        assert not ok


def test_hosts_with_non_dns_characters_are_rejected():
    for host in ("pib.gov\t.in", "pib.gov.in%2f@evil.com", "pib .gov.in",
                 "pib\n.gov.in", "under_score.pib.gov.in"):
        ok, _ = validate_url_for_fetch(f"https://{host}/x", APPROVED)
        assert not ok, host


def test_valid_hosts_still_pass_the_syntax_gate(monkeypatch):
    """The stricter host check must not break real approved URLs."""
    _public_dns(monkeypatch)
    for url in ("https://pib.gov.in/x", "https://static.pib.gov.in/a/b.pdf",
                "https://PIB.GOV.IN./x", "https://a-b.pib.gov.in/x"):
        ok, reason = validate_url_for_fetch(url, APPROVED)
        assert ok, f"{url}: {reason}"
